from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
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


class MigrateModelsToProviderModelCommandTests(TestCase):
    """Step 1 of the ModelConfig -> ProviderModel migration (expand-
    migrate-contract). Never touches ModelConfig or the old allowed_
    models/model_used fields - only ever asserts on the new parallel
    ones (Plan.allowed_provider_models, Message.provider_model_used)."""

    def setUp(self):
        from accounts.models import User
        from chat.models import Conversation, Message, ModelConfig
        from governance.models import Plan

        self.ModelConfig = ModelConfig
        self.Plan = Plan
        self.Message = Message

        self.gpt4o = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="gpt-4o",
            display_name="GPT-4o",
            tier=ModelConfig.Tier.PREMIUM,
            input_cost_per_1m=2.5,
            output_cost_per_1m=10,
            is_enabled=True,
        )
        self.gpt35 = ModelConfig.objects.create(
            provider=ModelConfig.Provider.OPENAI,
            model_name="gpt-3.5-turbo",
            is_enabled=False,  # deliberately disabled - must stay disabled after migration
        )
        self.plan = Plan.objects.create(name="TestPlan")
        self.plan.allowed_models.add(self.gpt4o, self.gpt35)

        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.conversation = Conversation.objects.create(user=self.user, title="t")
        self.message = Message.objects.create(
            conversation=self.conversation, role=Message.Role.ASSISTANT, content="hi", model_used=self.gpt4o
        )

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command("migrate_models_to_provider_model", stdout=out)
        self.assertEqual(ProviderModel.objects.count(), 0)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.allowed_provider_models.count(), 0)
        self.message.refresh_from_db()
        self.assertIsNone(self.message.provider_model_used)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("would create", out.getvalue())

    def test_apply_creates_provider_models_carrying_over_state_exactly(self):
        call_command("migrate_models_to_provider_model", "--apply")
        pm_4o = ProviderModel.objects.get(provider__slug="openai", model_id="gpt-4o")
        pm_35 = ProviderModel.objects.get(provider__slug="openai", model_id="gpt-3.5-turbo")
        # Grandfathering: is_enabled carried over exactly, not reset to
        # the fresh-discovery default of False - true for BOTH the
        # enabled and the deliberately-disabled model.
        self.assertTrue(pm_4o.is_enabled)
        self.assertFalse(pm_35.is_enabled)
        self.assertEqual(pm_4o.tier, "premium")
        self.assertFalse(pm_4o.is_new)

    def test_apply_populates_plan_allowed_provider_models_matching_exactly(self):
        call_command("migrate_models_to_provider_model", "--apply")
        self.plan.refresh_from_db()
        old_names = set(self.plan.allowed_models.values_list("model_name", flat=True))
        new_names = set(self.plan.allowed_provider_models.values_list("model_id", flat=True))
        self.assertEqual(old_names, new_names)
        # The OLD field is completely untouched.
        self.assertEqual(self.plan.allowed_models.count(), 2)

    def test_apply_populates_message_provider_model_used_with_zero_left_null(self):
        call_command("migrate_models_to_provider_model", "--apply")
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.provider_model_used)
        self.assertEqual(self.message.provider_model_used.model_id, "gpt-4o")
        # The OLD field is completely untouched.
        self.assertEqual(self.message.model_used_id, self.gpt4o.id)
        broken = self.Message.objects.filter(model_used__isnull=False, provider_model_used__isnull=True)
        self.assertEqual(broken.count(), 0)

    def test_apply_is_idempotent_no_duplicate_provider_models(self):
        call_command("migrate_models_to_provider_model", "--apply")
        call_command("migrate_models_to_provider_model", "--apply")
        self.assertEqual(ProviderModel.objects.count(), 2)

    def test_apply_reuses_an_already_existing_provider_model_instead_of_duplicating(self):
        # Simulates a model already connected/synced through the real
        # Providers flow before this migration ever runs.
        provider = Provider.objects.get(slug="openai")
        pre_existing = ProviderModel.objects.create(provider=provider, model_id="gpt-4o", is_enabled=True)
        call_command("migrate_models_to_provider_model", "--apply")
        self.assertEqual(ProviderModel.objects.filter(provider=provider, model_id="gpt-4o").count(), 1)
        self.message.refresh_from_db()
        self.assertEqual(self.message.provider_model_used_id, pre_existing.id)

    def test_unmatched_provider_string_is_reported_and_skipped_not_crashed(self):
        # ModelConfig.provider is a closed TextChoices (openai/anthropic
        # only) so this can't happen through the admin UI - constructed
        # directly here to prove the "no Provider match" path is handled
        # rather than assumed impossible.
        weird = self.ModelConfig.objects.create(provider="not-a-real-provider", model_name="mystery-model")
        self.plan.allowed_models.add(weird)
        out = StringIO()
        call_command("migrate_models_to_provider_model", "--apply", stdout=out)
        self.assertIn("NO PROVIDER MATCH", out.getvalue())
        self.assertFalse(ProviderModel.objects.filter(model_id="mystery-model").exists())
        # Everything else still gets mapped correctly despite the one bad row.
        self.assertEqual(ProviderModel.objects.filter(model_id="gpt-4o").count(), 1)


class ProviderListViewTests(TestCase):
    def test_requires_login(self):
        response = self.client.get(reverse("providers:list"))
        self.assertEqual(response.status_code, 302)

    def test_non_superadmin_roles_are_forbidden(self):
        for role in [User.Role.USER, User.Role.MANAGER, User.Role.ADMIN]:
            user = User.objects.create_user(email=f"{role}@example.com", password="pw12345!", role=role)
            self.client.force_login(user)
            response = self.client.get(reverse("providers:list"))
            self.assertEqual(response.status_code, 403, role)
            self.client.logout()

    def test_superadmin_sees_all_seeded_providers(self):
        superadmin = User.objects.create_user(email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN)
        self.client.force_login(superadmin)
        response = self.client.get(reverse("providers:list"))
        self.assertEqual(response.status_code, 200)
        for name in ["Anthropic", "OpenAI", "Google Gemini", "xAI Grok", "DeepSeek"]:
            self.assertContains(response, name)


class ConnectProviderViewTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.client.force_login(self.superadmin)
        self.provider = Provider.objects.get(slug="anthropic")

    def test_empty_key_is_rejected(self):
        response = self.client.post(
            reverse("providers:connect", kwargs={"provider_id": self.provider.id}), {"api_key": ""}
        )
        self.assertEqual(response.status_code, 400)
        self.provider.refresh_from_db()
        self.assertFalse(self.provider.is_connected)

    @patch("providers.adapters.anthropic.AnthropicAdapter.test_connection", return_value=False)
    def test_a_key_that_fails_verification_is_never_saved(self, mock_test):
        response = self.client.post(
            reverse("providers:connect", kwargs={"provider_id": self.provider.id}), {"api_key": "sk-bad-key"}
        )
        self.assertRedirects(response, reverse("providers:list"))
        self.provider.refresh_from_db()
        self.assertFalse(self.provider.is_connected)
        self.assertIsNone(self.provider.get_decrypted_key())

    @patch("providers.views.sync_provider")
    @patch("providers.adapters.anthropic.AnthropicAdapter.test_connection", return_value=True)
    def test_a_verified_key_connects_and_triggers_an_immediate_sync(self, mock_test, mock_sync):
        mock_sync.return_value = {
            "success": True,
            "new_count": 2,
            "updated_count": 0,
            "retired_count": 0,
            "error": None,
        }
        response = self.client.post(
            reverse("providers:connect", kwargs={"provider_id": self.provider.id}), {"api_key": "sk-realvalue1234"}
        )
        self.assertRedirects(response, reverse("providers:list"))
        self.provider.refresh_from_db()
        self.assertTrue(self.provider.is_connected)
        self.assertEqual(self.provider.get_decrypted_key(), "sk-realvalue1234")
        self.assertEqual(self.provider.api_key_last4, "1234")
        mock_sync.assert_called_once_with(self.provider)

    @patch("providers.views.sync_provider")
    @patch("providers.adapters.anthropic.AnthropicAdapter.test_connection", return_value=True)
    def test_raw_key_never_appears_in_the_audit_log(self, mock_test, mock_sync):
        from governance.models import AuditLog

        mock_sync.return_value = {
            "success": True,
            "new_count": 0,
            "updated_count": 0,
            "retired_count": 0,
            "error": None,
        }
        raw_key = "sk-averyuniquesecretvalue7777"
        self.client.post(reverse("providers:connect", kwargs={"provider_id": self.provider.id}), {"api_key": raw_key})
        self.assertTrue(AuditLog.objects.filter(action_type="provider.connect").exists())
        for log in AuditLog.objects.all():
            self.assertNotIn(raw_key, log.old_value)
            self.assertNotIn(raw_key, log.new_value)

    def test_non_superadmin_cannot_connect(self):
        self.client.logout()
        admin = User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.post(
            reverse("providers:connect", kwargs={"provider_id": self.provider.id}), {"api_key": "sk-x"}
        )
        self.assertEqual(response.status_code, 403)
        self.provider.refresh_from_db()
        self.assertFalse(self.provider.is_connected)


class ResyncProviderViewTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.client.force_login(self.superadmin)
        self.provider = Provider.objects.get(slug="anthropic")

    def test_resync_without_a_connected_key_is_a_clean_no_op(self):
        response = self.client.post(reverse("providers:resync", kwargs={"provider_id": self.provider.id}))
        self.assertRedirects(response, reverse("providers:list"))
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.last_sync_status, Provider.SyncStatus.NEVER)

    @patch("providers.views.sync_provider")
    def test_resync_when_connected_calls_sync_provider_and_returns_the_card_partial(self, mock_sync):
        self.provider.set_api_key("sk-key")
        self.provider.is_connected = True
        self.provider.save()
        mock_sync.return_value = {
            "success": True,
            "new_count": 1,
            "updated_count": 0,
            "retired_count": 0,
            "error": None,
        }
        response = self.client.post(
            reverse("providers:resync", kwargs={"provider_id": self.provider.id}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once_with(self.provider)
        self.assertIn(f'id="provider-card-{self.provider.id}"'.encode(), response.content)
        # A full-page (non-htmx) request never renders a bare partial - no <html> chrome either way here.
        self.assertNotIn(b"<html", response.content)


class DisconnectProviderViewTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.client.force_login(self.superadmin)
        self.provider = Provider.objects.get(slug="anthropic")
        self.provider.set_api_key("sk-key")
        self.provider.is_connected = True
        self.provider.save()
        self.enabled_model = ProviderModel.objects.create(
            provider=self.provider, model_id="claude-x", is_enabled=True, is_new=False
        )
        self.disabled_model = ProviderModel.objects.create(
            provider=self.provider, model_id="claude-y", is_enabled=False, is_new=False
        )

    def test_disconnect_clears_the_key_and_disables_every_enabled_model(self):
        response = self.client.post(reverse("providers:disconnect", kwargs={"provider_id": self.provider.id}))
        self.assertRedirects(response, reverse("providers:list"))
        self.provider.refresh_from_db()
        self.assertFalse(self.provider.is_connected)
        self.assertIsNone(self.provider.get_decrypted_key())
        self.enabled_model.refresh_from_db()
        self.assertFalse(self.enabled_model.is_enabled)
        # Never deleted - historical Message/Plan references must keep resolving.
        self.assertTrue(ProviderModel.objects.filter(id=self.enabled_model.id).exists())

    def test_disconnect_does_not_touch_an_already_disabled_model(self):
        self.client.post(reverse("providers:disconnect", kwargs={"provider_id": self.provider.id}))
        self.disabled_model.refresh_from_db()
        self.assertFalse(self.disabled_model.is_enabled)


class ToggleProviderModelViewTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.client.force_login(self.superadmin)
        self.provider = Provider.objects.get(slug="openai")
        self.model = ProviderModel.objects.create(
            provider=self.provider, model_id="gpt-x", is_enabled=False, is_new=True
        )

    def test_toggle_enables_and_marks_reviewed(self):
        response = self.client.post(reverse("providers:toggle_model", kwargs={"model_id": self.model.id}))
        self.assertRedirects(response, reverse("providers:list"))
        self.model.refresh_from_db()
        self.assertTrue(self.model.is_enabled)
        self.assertFalse(self.model.is_new)

    def test_toggling_again_disables_it(self):
        self.model.is_enabled = True
        self.model.save()
        self.client.post(reverse("providers:toggle_model", kwargs={"model_id": self.model.id}))
        self.model.refresh_from_db()
        self.assertFalse(self.model.is_enabled)

    def test_non_superadmin_cannot_toggle(self):
        self.client.logout()
        admin = User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.post(reverse("providers:toggle_model", kwargs={"model_id": self.model.id}))
        self.assertEqual(response.status_code, 403)
        self.model.refresh_from_db()
        self.assertFalse(self.model.is_enabled)


class SyncAllConnectedProvidersTaskTests(TestCase):
    """The daily sync_all_connected_providers Celery task (providers/
    tasks.py) - mirrors chat.tasks.check_for_new_models' notify pattern,
    but drives real ProviderModel discovery via sync_provider() (still
    bound by its own never-auto-enable guardrail) instead of only
    checking."""

    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.connected = Provider.objects.get(slug="openai")
        self.connected.set_api_key("sk-key")
        self.connected.is_connected = True
        self.connected.save()
        self.disconnected = Provider.objects.get(slug="anthropic")

    def test_only_connected_providers_are_synced(self):
        from providers.tasks import sync_all_connected_providers

        with patch("providers.services.sync_provider") as mock_sync:
            mock_sync.return_value = {
                "success": True,
                "new_count": 0,
                "updated_count": 0,
                "retired_count": 0,
                "error": None,
            }
            sync_all_connected_providers()
        mock_sync.assert_called_once_with(self.connected)

    def test_notifies_admins_when_new_models_found(self):
        from notifications.models import Notification, NotificationType
        from providers.tasks import sync_all_connected_providers

        with patch("providers.services.sync_provider") as mock_sync:
            mock_sync.return_value = {
                "success": True,
                "new_count": 3,
                "updated_count": 0,
                "retired_count": 0,
                "error": None,
            }
            sync_all_connected_providers()
        self.assertTrue(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).exists()
        )

    def test_does_not_notify_when_nothing_new(self):
        from notifications.models import Notification, NotificationType
        from providers.tasks import sync_all_connected_providers

        with patch("providers.services.sync_provider") as mock_sync:
            mock_sync.return_value = {
                "success": True,
                "new_count": 0,
                "updated_count": 1,
                "retired_count": 0,
                "error": None,
            }
            sync_all_connected_providers()
        self.assertFalse(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).exists()
        )

    def test_does_not_notify_when_sync_failed(self):
        from notifications.models import Notification, NotificationType
        from providers.tasks import sync_all_connected_providers

        with patch("providers.services.sync_provider") as mock_sync:
            mock_sync.return_value = {
                "success": False,
                "new_count": 0,
                "updated_count": 0,
                "retired_count": 0,
                "error": "boom",
            }
            sync_all_connected_providers()
        self.assertFalse(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).exists()
        )

    def test_dedup_prevents_repeat_notification_within_a_day(self):
        from notifications.models import Notification, NotificationType
        from providers.tasks import sync_all_connected_providers

        with patch("providers.services.sync_provider") as mock_sync:
            mock_sync.return_value = {
                "success": True,
                "new_count": 1,
                "updated_count": 0,
                "retired_count": 0,
                "error": None,
            }
            sync_all_connected_providers()
            sync_all_connected_providers()
        self.assertEqual(
            Notification.objects.filter(
                user=self.admin, notification_type=NotificationType.MODEL_SYNC_AVAILABLE
            ).count(),
            1,
        )


class SeedProviderResyncScheduleMigrationTests(TestCase):
    def test_periodic_task_is_registered(self):
        from django_celery_beat.models import PeriodicTask

        task = PeriodicTask.objects.get(name="Daily connected-provider model resync")
        self.assertEqual(task.task, "providers.tasks.sync_all_connected_providers")
        self.assertTrue(task.enabled)
