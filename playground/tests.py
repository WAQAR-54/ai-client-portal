from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from governance.models import RoleFeatureToggle
from playground.models import PlaygroundRun
from playground.views import DAILY_RUN_LIMIT
from providers.models import Provider, ProviderModel


class PlaygroundAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="dev@example.com", password="pw12345!")

    def test_disabled_by_default_returns_403(self):
        # No RoleFeatureToggle row seeded for this test DB unless the
        # governance migration ran - explicitly assert the off state so
        # this test doesn't silently depend on migration state.
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="code_playground", defaults={"is_enabled": False}
        )
        self.client.login(email="dev@example.com", password="pw12345!")
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 403)

    def test_enabled_role_can_access_and_sees_playground_enabled_models(self):
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="code_playground", defaults={"is_enabled": True}
        )
        openai = Provider.objects.get(slug="openai")
        ProviderModel.objects.create(provider=openai, model_id="gpt-5", is_enabled=True, is_playground_enabled=True)
        # Enabled for chat but NOT opted into Playground - must not appear.
        ProviderModel.objects.create(provider=openai, model_id="gpt-4", is_enabled=True, is_playground_enabled=False)

        self.client.login(email="dev@example.com", password="pw12345!")
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "gpt-5")
        self.assertNotContains(response, "gpt-4")

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_logging_in_from_the_playground_link_lands_back_on_playground(self):
        """The reported bug: opening the shared /playground/ link while
        logged out redirected to login, but logging in from there dumped
        the user on the normal dashboard/chat instead of back on
        Playground - see accounts.views.PortalLoginView.get_success_url."""
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="code_playground", defaults={"is_enabled": True}
        )
        redirect_response = self.client.get(reverse("playground:home"))
        login_url_with_next = redirect_response.url

        login_response = self.client.post(login_url_with_next, {"username": "dev@example.com", "password": "pw12345!"})
        self.assertRedirects(login_response, reverse("playground:home"))

    def test_anonymous_sees_a_login_prompt_explaining_why(self):
        response = self.client.get(reverse("playground:home"), follow=True)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("log in" in m.lower() for m in messages), messages)

    def test_logged_in_but_lacking_access_gets_403_not_a_login_prompt(self):
        # A real 403 (role denial), not the anonymous login nudge above -
        # handle_no_permission must only fire for the logged-out case.
        self.client.login(email="dev@example.com", password="pw12345!")
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 403)

    def test_superadmin_always_has_access_even_without_a_toggle_row(self):
        User.objects.create_user(
            email="super@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_staff=True
        )
        self.client.login(email="super@example.com", password="pw12345!")
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 200)

    def test_admin_has_access_by_default_with_no_toggle_row(self):
        """Admin can open Playground to check it by default (no row =
        visible, same rule as every other feature) - per the admin
        Dashboard's own "Open Code Playground" link. A SuperAdmin can
        still explicitly turn this off for the admin role, same as
        User/Manager - see test_superadmin_can_disable_it_for_admin_too."""
        User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 200)

    def test_superadmin_can_disable_it_for_admin_too(self):
        User.objects.create_user(email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True)
        RoleFeatureToggle.objects.create(role="admin", feature_key="code_playground", is_enabled=False)
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("playground:home"))
        self.assertEqual(response.status_code, 403)


class LogRunTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="dev@example.com", password="pw12345!")
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="code_playground", defaults={"is_enabled": True}
        )
        self.client.login(email="dev@example.com", password="pw12345!")

    def test_log_run_creates_row_and_returns_remaining_quota(self):
        response = self.client.post(reverse("playground:log_run"), {"language": "python"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["allowed"])
        self.assertEqual(data["remaining"], DAILY_RUN_LIMIT - 1)
        self.assertEqual(PlaygroundRun.objects.filter(user=self.user).count(), 1)

    def test_log_run_blocked_once_daily_limit_reached(self):
        for _ in range(DAILY_RUN_LIMIT):
            PlaygroundRun.objects.create(user=self.user)
        response = self.client.post(reverse("playground:log_run"), {"language": "python"})
        self.assertEqual(response.status_code, 429)
        self.assertFalse(response.json()["allowed"])
        # The blocked attempt itself must not be logged.
        self.assertEqual(PlaygroundRun.objects.filter(user=self.user).count(), DAILY_RUN_LIMIT)

    def test_log_run_requires_the_feature_to_be_enabled(self):
        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.USER, feature_key="code_playground", defaults={"is_enabled": False}
        )
        response = self.client.post(reverse("playground:log_run"), {"language": "python"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_language_falls_back_to_python(self):
        response = self.client.post(reverse("playground:log_run"), {"language": "not-a-real-language"})
        self.assertEqual(response.status_code, 200)
        run = PlaygroundRun.objects.get(user=self.user)
        self.assertEqual(run.language, PlaygroundRun.Language.PYTHON)
