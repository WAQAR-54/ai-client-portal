from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from chat.providers import ProviderError, StreamChunk
from domaingen.models import DomainSearch
from domaingen.views import DAILY_SEARCH_LIMIT, _parse_suggestions
from governance.models import RoleFeatureToggle
from providers.models import Provider, ProviderModel


class WhoisQueryTargetTests(TestCase):
    """Regression guard: check_domain_available must query the FULL
    "name.tld" over the wire, not the bare label - querying WHOIS for just
    "google" (no TLD) returns a registry "no match" response for almost
    any label, since no such unqualified record exists, which read as a
    false "available" for every single suggestion before this was fixed."""

    @patch("domaingen.whois._raw_whois_query")
    def test_queries_the_full_domain_not_just_the_name(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = "Domain Name: EXAMPLE.COM"
        check_domain_available("example", "com")
        queried_domain = mock_query.call_args[0][1]
        self.assertEqual(queried_domain, "example.com")


class WhoisRegistryFormatTests(TestCase):
    """PKNIC (.pk) doesn't use a "Domain Name:" field at all, unlike every
    other supported registry - these lock in the real response shapes
    observed from each live registry (see domaingen/whois.py's indicator
    comments) so a future indicator-list edit can't silently break one of
    them without a test noticing."""

    @patch("domaingen.whois._raw_whois_query")
    def test_pknic_taken_format(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = (
            "# WHOIS .PK Domains (PKNIC)\n\n    Domain: google.com.pk\n    Status: Domain is Registered"
        )
        self.assertFalse(check_domain_available("google.com", "pk"))

    @patch("domaingen.whois._raw_whois_query")
    def test_pknic_available_format(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = (
            "# WHOIS .PK Domains (PKNIC)\n\n    Domain: something.pk\n"
            "    Status: Not Registered, and may be available if valid\n    Available: Yes."
        )
        self.assertTrue(check_domain_available("something", "pk"))

    @patch("domaingen.whois._raw_whois_query")
    def test_nominet_uk_taken_format(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = "    Domain name:\n        google.co.uk\n\n    Registered on: 14-Feb-1999"
        self.assertFalse(check_domain_available("google.co", "uk"))

    @patch("domaingen.whois._raw_whois_query")
    def test_nominet_uk_available_format(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = '    No match for "something.uk".\n\n    This domain name has not been registered.'
        self.assertTrue(check_domain_available("something", "uk"))

    @patch("domaingen.whois._raw_whois_query")
    def test_dotco_taken_format(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = "Domain Name: GOOGLE.CO\nRegistry Domain ID: D157997-CNIC"
        self.assertFalse(check_domain_available("google", "co"))

    @patch("domaingen.whois._raw_whois_query")
    def test_dotco_available_format(self, mock_query):
        from domaingen.whois import check_domain_available

        mock_query.return_value = "The queried object does not exist: DOMAIN NOT FOUND"
        self.assertTrue(check_domain_available("something", "co"))


class DomainGeneratorAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="dev@example.com", password="pw12345!")

    def test_disabled_by_default_returns_403(self):
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="domain_generator", defaults={"is_enabled": False}
        )
        self.client.login(email="dev@example.com", password="pw12345!")
        response = self.client.get(reverse("domaingen:home"))
        self.assertEqual(response.status_code, 403)

    def test_enabled_role_can_access_and_sees_enabled_models_only(self):
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="domain_generator", defaults={"is_enabled": True}
        )
        openai = Provider.objects.get(slug="openai")
        ProviderModel.objects.create(
            provider=openai, model_id="gpt-5", is_enabled=True, is_domain_generator_enabled=True
        )
        ProviderModel.objects.create(
            provider=openai, model_id="gpt-4", is_enabled=True, is_domain_generator_enabled=False
        )

        self.client.login(email="dev@example.com", password="pw12345!")
        response = self.client.get(reverse("domaingen:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "gpt-5")
        self.assertNotContains(response, "gpt-4")

    def test_anonymous_redirected_with_login_prompt(self):
        response = self.client.get(reverse("domaingen:home"), follow=True)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("log in" in m.lower() for m in messages), messages)

    def test_superadmin_always_has_access(self):
        User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.client.login(email="super@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("domaingen:home")).status_code, 200)

    def test_admin_has_access_by_default_with_no_toggle_row(self):
        User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True)
        self.client.login(email="admin@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("domaingen:home")).status_code, 200)

    def test_superadmin_can_disable_it_for_admin_too(self):
        User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True)
        RoleFeatureToggle.objects.create(role="admin", feature_key="domain_generator", is_enabled=False)
        self.client.login(email="admin@example.com", password="pw12345!")
        self.assertEqual(self.client.get(reverse("domaingen:home")).status_code, 403)


class ParseSuggestionsTests(TestCase):
    def test_parses_valid_json_array(self):
        raw = '[{"name": "legaldesk", "tld": "com"}, {"name": "lawcopilot", "tld": "co"}]'
        result = _parse_suggestions(raw, "all")
        self.assertEqual(
            result,
            [
                {"name": "legaldesk", "tld": "com", "domain": "legaldesk.com"},
                {"name": "lawcopilot", "tld": "co", "domain": "lawcopilot.co"},
            ],
        )

    def test_strips_markdown_fence(self):
        raw = '```json\n[{"name": "legaldesk", "tld": "com"}]\n```'
        result = _parse_suggestions(raw, "all")
        self.assertEqual(result, [{"name": "legaldesk", "tld": "com", "domain": "legaldesk.com"}])

    def test_invalid_json_returns_empty(self):
        self.assertEqual(_parse_suggestions("not json at all", "all"), [])

    def test_non_list_json_returns_empty(self):
        self.assertEqual(_parse_suggestions('{"name": "legaldesk"}', "all"), [])

    def test_filters_out_tld_not_matching_the_requested_filter(self):
        raw = '[{"name": "legaldesk", "tld": "com"}, {"name": "lawcopilot", "tld": "co"}]'
        result = _parse_suggestions(raw, "com")
        self.assertEqual(result, [{"name": "legaldesk", "tld": "com", "domain": "legaldesk.com"}])

    def test_filters_out_malformed_names(self):
        raw = '[{"name": "has spaces", "tld": "com"}, {"name": "okname", "tld": "com"}, {"name": "UP", "tld": "com"}]'
        result = _parse_suggestions(raw, "all")
        self.assertEqual(result, [{"name": "okname", "tld": "com", "domain": "okname.com"}])

    def test_dedupes_identical_domains(self):
        raw = '[{"name": "legaldesk", "tld": "com"}, {"name": "legaldesk", "tld": "com"}]'
        result = _parse_suggestions(raw, "all")
        self.assertEqual(len(result), 1)

    def test_skips_non_dict_items(self):
        raw = '["just a string", {"name": "okname", "tld": "com"}]'
        result = _parse_suggestions(raw, "all")
        self.assertEqual(result, [{"name": "okname", "tld": "com", "domain": "okname.com"}])


class GenerateDomainsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="dev@example.com", password="pw12345!")
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="domain_generator", defaults={"is_enabled": True}
        )
        self.model = ProviderModel.objects.create(
            provider=Provider.objects.get(slug="openai"),
            model_id="gpt-5",
            is_enabled=True,
            is_domain_generator_enabled=True,
            input_price_per_mtok=1,
            output_price_per_mtok=2,
        )
        self.client.login(email="dev@example.com", password="pw12345!")

    def _post(self, **overrides):
        data = {"query": "an AI portal for small law firms", "tld": "all", "provider_model_id": self.model.id}
        data.update(overrides)
        return self.client.post(reverse("domaingen:generate"), data)

    @patch("domaingen.views.check_domain_available")
    @patch("chat.providers.get_provider")
    def test_successful_generation_logs_search_and_returns_results(self, mock_get_provider, mock_check_available):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.return_value = iter(
            [
                StreamChunk(text='[{"name": "legaldesk", "tld": "com"}]'),
                StreamChunk(done=True, input_tokens=100, output_tokens=50),
            ]
        )
        mock_check_available.return_value = True

        response = self._post()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["results"], [{"domain": "legaldesk.com", "available": True}])
        self.assertEqual(data["remaining"], DAILY_SEARCH_LIMIT - 1)
        self.assertIsNotNone(data["cost_display"])

        search = DomainSearch.objects.get(user=self.user)
        self.assertEqual(search.provider_model, self.model)
        self.assertEqual(search.input_tokens, 100)
        self.assertEqual(search.output_tokens, 50)
        self.assertIsNotNone(search.estimated_cost)

    @patch("domaingen.views.check_domain_available")
    @patch("chat.providers.get_provider")
    def test_taken_domain_result(self, mock_get_provider, mock_check_available):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.return_value = iter(
            [
                StreamChunk(text='[{"name": "google", "tld": "com"}]'),
                StreamChunk(done=True, input_tokens=10, output_tokens=5),
            ]
        )
        mock_check_available.return_value = False

        data = self._post().json()
        self.assertEqual(data["results"], [{"domain": "google.com", "available": False}])

    def test_query_too_short_returns_400_without_calling_ai(self):
        with patch("chat.providers.get_provider") as mock_get_provider:
            response = self._post(query="ab")
            mock_get_provider.assert_not_called()
        self.assertEqual(response.status_code, 400)

    def test_missing_model_returns_400(self):
        response = self._post(provider_model_id=999999)
        self.assertEqual(response.status_code, 400)

    @patch("chat.providers.get_provider")
    def test_provider_error_returns_502(self, mock_get_provider):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.side_effect = ProviderError("upstream down")
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(DomainSearch.objects.exists())

    @patch("chat.providers.get_provider")
    def test_unexpected_exception_during_generation_returns_json_not_html(self, mock_get_provider):
        """Regression guard: an uncaught exception here used to render
        Django's HTML error page, which the frontend's fetch().then(r =>
        r.json()) can't parse - it throws, and the user sees a generic
        "Network error" with no indication anything AI-related broke."""
        mock_get_provider.side_effect = RuntimeError("boom")
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertIn("error", response.json())

    @patch("domaingen.views.check_domain_available")
    @patch("chat.providers.get_provider")
    def test_whois_pool_failure_degrades_to_unknown_instead_of_failing_the_search(
        self, mock_get_provider, mock_check_available
    ):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.return_value = iter(
            [
                StreamChunk(text='[{"name": "legaldesk", "tld": "com"}]'),
                StreamChunk(done=True, input_tokens=10, output_tokens=5),
            ]
        )
        mock_check_available.side_effect = RuntimeError("network unreachable")
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["available"], None)

    @patch("chat.providers.get_provider")
    def test_unparseable_ai_response_returns_502_and_does_not_log_a_search(self, mock_get_provider):
        mock_provider = mock_get_provider.return_value
        mock_provider.stream_chat.return_value = iter(
            [
                StreamChunk(text="I cannot do that."),
                StreamChunk(done=True, input_tokens=5, output_tokens=5),
            ]
        )
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(DomainSearch.objects.exists())

    @patch("domaingen.views.check_domain_available")
    @patch("chat.providers.get_provider")
    def test_daily_limit_blocks_further_generation(self, mock_get_provider, mock_check_available):
        for _ in range(DAILY_SEARCH_LIMIT):
            DomainSearch.objects.create(user=self.user, query="x")
        response = self._post()
        self.assertEqual(response.status_code, 429)
        mock_get_provider.assert_not_called()

    def test_requires_login(self):
        self.client.logout()
        response = self._post()
        self.assertEqual(response.status_code, 302)

    def test_403_when_feature_disabled(self):
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="domain_generator", defaults={"is_enabled": False}
        )
        response = self._post()
        self.assertEqual(response.status_code, 403)
