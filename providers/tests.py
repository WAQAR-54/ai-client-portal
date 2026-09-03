from unittest.mock import patch

from django.test import TestCase

from providers.adapters import get_adapter_class
from providers.adapters.anthropic import AnthropicAdapter
from providers.adapters.gemini import GeminiAdapter
from providers.adapters.openai_compatible import OpenAICompatibleAdapter
from providers.models import Provider, ProviderModel
from providers.services import sync_provider


class ProviderEncryptionTests(TestCase):
    """The one property that matters most here: the raw key is never
    recoverable from anything except get_decrypted_key(), and what's
    stored on the row for display is only ever the last 4 characters."""

    def test_set_and_get_decrypted_key_round_trips(self):
        provider = Provider.objects.create(name="OpenAI", slug="openai-t1", adapter_type="openai_compatible")
        provider.set_api_key("sk-verysecretvalue1234")
        provider.save()
        provider.refresh_from_db()
        self.assertEqual(provider.get_decrypted_key(), "sk-verysecretvalue1234")

    def test_last4_is_stored_for_masked_display(self):
        provider = Provider.objects.create(name="OpenAI", slug="openai-t2", adapter_type="openai_compatible")
        provider.set_api_key("sk-verysecretvalue1234")
        self.assertEqual(provider.api_key_last4, "1234")

    def test_encrypted_field_never_contains_the_raw_key(self):
        provider = Provider.objects.create(name="OpenAI", slug="openai-t3", adapter_type="openai_compatible")
        raw_key = "sk-averydistinctivesecretvalue9999"
        provider.set_api_key(raw_key)
        provider.save()
        provider.refresh_from_db()
        self.assertNotIn(raw_key.encode(), bytes(provider.api_key_encrypted))

    def test_get_decrypted_key_is_none_when_never_connected(self):
        provider = Provider.objects.create(name="OpenAI", slug="openai-t4", adapter_type="openai_compatible")
        self.assertIsNone(provider.get_decrypted_key())


class AdapterFactoryTests(TestCase):
    def test_get_adapter_class_resolves_each_registered_type(self):
        self.assertIs(get_adapter_class("anthropic"), AnthropicAdapter)
        self.assertIs(get_adapter_class("openai_compatible"), OpenAICompatibleAdapter)
        self.assertIs(get_adapter_class("gemini"), GeminiAdapter)

    def test_unknown_adapter_type_raises(self):
        with self.assertRaises(ValueError):
            get_adapter_class("not-a-real-adapter")

    def test_provider_get_adapter_returns_correct_instance_bound_to_itself(self):
        provider = Provider.objects.create(name="Anthropic", slug="anthropic-t1", adapter_type="anthropic")
        adapter = provider.get_adapter()
        self.assertIsInstance(adapter, AnthropicAdapter)
        self.assertIs(adapter.provider, provider)


class SyncProviderTests(TestCase):
    """The guardrail that matters most in the whole feature: a freshly
    discovered model is never auto-enabled, and an admin's own enable/
    disable choice survives every later resync untouched."""

    def setUp(self):
        self.provider = Provider.objects.create(name="OpenAI", slug="openai-sync1", adapter_type="openai_compatible")
        self.provider.set_api_key("sk-testkey1234")
        self.provider.is_connected = True
        self.provider.save()

    def _fake_fetch(self, models):
        return patch(
            "providers.adapters.openai_compatible.OpenAICompatibleAdapter.fetch_models",
            return_value=models,
        )

    def test_newly_discovered_model_is_disabled_by_default(self):
        with self._fake_fetch(
            [{"model_id": "gpt-5", "display_name": "GPT-5", "input_price": None, "output_price": None}]
        ):
            result = sync_provider(self.provider)
        self.assertTrue(result["success"])
        self.assertEqual(result["new_count"], 1)
        model = ProviderModel.objects.get(provider=self.provider, model_id="gpt-5")
        self.assertFalse(model.is_enabled)
        self.assertTrue(model.is_new)

    def test_resync_never_re_enables_or_disables_an_admin_reviewed_model(self):
        model = ProviderModel.objects.create(provider=self.provider, model_id="gpt-5", is_enabled=True, is_new=False)
        with self._fake_fetch(
            [{"model_id": "gpt-5", "display_name": "GPT-5 v2", "input_price": None, "output_price": None}]
        ):
            result = sync_provider(self.provider)
        self.assertEqual(result["updated_count"], 1)
        model.refresh_from_db()
        self.assertTrue(model.is_enabled)  # untouched by the resync
        self.assertEqual(model.display_name, "GPT-5 v2")  # display/pricing still refreshes

    def test_model_no_longer_listed_is_retired_and_force_disabled(self):
        model = ProviderModel.objects.create(provider=self.provider, model_id="gpt-4-old", is_enabled=True)
        with self._fake_fetch([]):
            result = sync_provider(self.provider)
        self.assertEqual(result["retired_count"], 1)
        model.refresh_from_db()
        self.assertTrue(model.is_retired)
        self.assertFalse(model.is_enabled)
        # Never deleted - historical Message/Plan references must keep resolving.
        self.assertTrue(ProviderModel.objects.filter(id=model.id).exists())

    def test_retired_model_reappearing_is_un_retired_but_still_not_re_enabled(self):
        model = ProviderModel.objects.create(
            provider=self.provider, model_id="gpt-5", is_enabled=False, is_retired=True
        )
        with self._fake_fetch(
            [{"model_id": "gpt-5", "display_name": "GPT-5", "input_price": None, "output_price": None}]
        ):
            sync_provider(self.provider)
        model.refresh_from_db()
        self.assertFalse(model.is_retired)
        self.assertFalse(model.is_enabled)

    def test_adapter_error_is_recorded_without_the_raw_key(self):
        from providers.adapters import ProviderAPIError as PAE

        with patch(
            "providers.adapters.openai_compatible.OpenAICompatibleAdapter.fetch_models",
            side_effect=PAE("401 Unauthorized for key sk-testkey1234"),
        ):
            result = sync_provider(self.provider)
        self.assertFalse(result["success"])
        self.assertNotIn("sk-testkey1234", result["error"])
        self.provider.refresh_from_db()
        self.assertNotIn("sk-testkey1234", self.provider.last_sync_error)
        self.assertEqual(self.provider.last_sync_status, Provider.SyncStatus.FAILED)

    def test_sync_without_a_connected_key_is_a_clean_no_op(self):
        provider = Provider.objects.create(name="Anthropic", slug="anthropic-sync1", adapter_type="anthropic")
        result = sync_provider(provider)
        self.assertFalse(result["success"])
        self.assertEqual(ProviderModel.objects.filter(provider=provider).count(), 0)


class SeedProvidersMigrationTests(TestCase):
    """Confirms the seed migration's actual effect on a real database,
    aware that these 5 rows exist as the test DB baseline (not assuming
    an empty Provider table - that exact wrong assumption is what broke
    ~34 existing tests the last time this project seeded via migration)."""

    def test_five_builtin_providers_exist_and_start_disconnected(self):
        self.assertEqual(Provider.objects.count(), 5)
        slugs = set(Provider.objects.values_list("slug", flat=True))
        self.assertEqual(slugs, {"anthropic", "openai", "gemini", "grok", "deepseek"})
        self.assertTrue(all(not p.is_connected for p in Provider.objects.all()))
        self.assertTrue(all(not p.api_key_encrypted for p in Provider.objects.all()))
