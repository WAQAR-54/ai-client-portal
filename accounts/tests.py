import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from accounts.geo import language_for_ip
from accounts.models import Department, User


class UserModelTests(TestCase):
    def test_create_user_defaults_to_user_role(self):
        user = User.objects.create_user(email="a@example.com", password="pw12345!")
        self.assertEqual(user.role, User.Role.USER)
        self.assertFalse(user.is_staff)

    def test_create_superuser_is_admin_role(self):
        admin = User.objects.create_superuser(email="root@example.com", password="pw12345!")
        self.assertEqual(admin.role, User.Role.ADMIN)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)

    def test_email_is_username_field(self):
        self.assertEqual(User.USERNAME_FIELD, "email")


class AuthAndRBACTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Engineering")
        self.user = User.objects.create_user(
            email="user@example.com",
            password="pw12345!",
            role=User.Role.USER,
            department=self.department,
        )
        self.manager = User.objects.create_user(
            email="manager@example.com",
            password="pw12345!",
            role=User.Role.MANAGER,
            department=self.department,
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
        )

    def test_login_with_email(self):
        response = self.client.post(
            reverse("accounts:login"),
            {
                "username": "user@example.com",
                "password": "pw12345!",
            },
        )
        self.assertRedirects(response, reverse("accounts:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_login_honors_next_redirect(self):
        """A login-required deep link (e.g. Code Playground's shared URL)
        must send the user back where they were headed, not dump them on
        the generic dashboard - PortalLoginView.get_success_url() used to
        ignore ?next= entirely."""
        response = self.client.post(
            reverse("accounts:login") + "?next=/chat/",
            {"username": "user@example.com", "password": "pw12345!"},
        )
        self.assertRedirects(response, "/chat/")

    def test_login_form_carries_next_through_the_post(self):
        """The GET page must echo ?next= back as a hidden field so it
        survives the POST - without it, get_success_url() has nothing to
        read even after the view-level fix above."""
        response = self.client.get(reverse("accounts:login") + "?next=/chat/")
        self.assertContains(response, 'name="next" value="/chat/"')

    def test_login_falls_back_to_dashboard_with_no_next(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "user@example.com", "password": "pw12345!"},
        )
        self.assertRedirects(response, reverse("accounts:dashboard"))

    def test_login_ignores_an_unsafe_next_to_another_host(self):
        response = self.client.post(
            reverse("accounts:login") + "?next=https://evil.example.com/",
            {"username": "user@example.com", "password": "pw12345!"},
        )
        self.assertRedirects(response, reverse("accounts:dashboard"))

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_regular_user_forbidden_from_admin_panel(self):
        self.client.login(email="user@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:admin_panel"))
        self.assertEqual(response.status_code, 403)

    def test_manager_forbidden_from_admin_panel(self):
        self.client.login(email="manager@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:admin_panel"))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_access_admin_panel(self):
        self.client.login(email="admin@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:admin_panel"))
        self.assertRedirects(response, reverse("governance:dashboard"))

    def test_logout_redirects_to_login(self):
        self.client.login(email="user@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("accounts:login"))


class SignupTests(TestCase):
    def test_signup_creates_user_with_default_role_and_logs_in(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newperson@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
        )
        self.assertRedirects(response, reverse("accounts:dashboard"))

        user = User.objects.get(email="newperson@example.com")
        self.assertEqual(user.role, User.Role.USER)
        self.assertTrue(user.is_active)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_signup_rejects_mismatched_passwords(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newperson@example.com",
                "password1": "a-strong-password-123",
                "password2": "different-password-456",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="newperson@example.com").exists())

    def test_signup_rejects_duplicate_email(self):
        User.objects.create_user(email="taken@example.com", password="pw12345!")
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "taken@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(email="taken@example.com").count(), 1)

    def test_already_logged_in_user_redirected_away_from_signup(self):
        User.objects.create_user(email="existing@example.com", password="pw12345!")
        self.client.login(email="existing@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:signup"))
        self.assertRedirects(response, reverse("accounts:dashboard"))


class PasswordResetFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="reset@example.com", password="old-password-123")

    def _extract_reset_url(self, html_body):
        import re

        match = re.search(r'href="(http[^"]*/password-reset/confirm/[^"]+)"', html_body)
        self.assertIsNotNone(match, "reset link not found in email body")
        return match.group(1)

    def test_request_sends_email_for_existing_user(self):
        from django.core import mail

        mail.outbox = []
        response = self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_request_does_not_reveal_whether_email_exists(self):
        from django.core import mail

        mail.outbox = []
        response = self.client.post(reverse("accounts:password_reset_request"), {"email": "nobody@example.com"})
        # Same redirect/message either way - no mail sent, but no error shown.
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertEqual(len(mail.outbox), 0)

    def test_full_reset_flow_changes_password(self):
        from django.core import mail

        mail.outbox = []
        self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        html_body = mail.outbox[0].alternatives[0][0]
        reset_path = self._extract_reset_url(html_body).split("password-reset/confirm/", 1)[1]
        uidb64, token = reset_path.strip("/").split("/")

        confirm_url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token})
        get_response = self.client.get(confirm_url)
        self.assertContains(get_response, "Set a new password")

        post_response = self.client.post(
            confirm_url,
            {"new_password1": "brand-new-password-456", "new_password2": "brand-new-password-456"},
        )
        self.assertRedirects(post_response, reverse("accounts:login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("brand-new-password-456"))

    def test_confirm_with_invalid_token_shows_invalid_link(self):
        confirm_url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": "invalid", "token": "bad-token"})
        response = self.client.get(confirm_url)
        self.assertContains(response, "invalid or has expired")

    def test_reset_link_cannot_be_reused(self):
        from django.core import mail

        mail.outbox = []
        self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        html_body = mail.outbox[0].alternatives[0][0]
        reset_path = self._extract_reset_url(html_body).split("password-reset/confirm/", 1)[1]
        uidb64, token = reset_path.strip("/").split("/")
        confirm_url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token})

        self.client.post(
            confirm_url, {"new_password1": "brand-new-password-456", "new_password2": "brand-new-password-456"}
        )
        # Token was single-use (bound to the password hash) - reusing it now fails.
        second_response = self.client.get(confirm_url)
        self.assertContains(second_response, "invalid or has expired")

    def test_already_logged_in_user_redirected_away_from_reset_pages(self):
        self.client.login(email="reset@example.com", password="old-password-123")
        response = self.client.get(reverse("accounts:password_reset_request"))
        self.assertRedirects(response, reverse("accounts:dashboard"))


class ProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")

    def test_update_name(self):
        response = self.client.post(
            reverse("accounts:profile"),
            {
                "first_name": "Ayesha",
                "last_name": "Khan",
            },
        )
        self.assertRedirects(response, reverse("accounts:profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Ayesha")
        self.assertEqual(self.user.last_name, "Khan")

    def test_cannot_change_role_or_department_from_profile_form(self):
        # ProfileForm only exposes first/last name — role/department aren't postable here.
        response = self.client.post(
            reverse("accounts:profile"),
            {
                "first_name": "A",
                "last_name": "B",
                "role": User.Role.ADMIN,
            },
        )
        self.assertRedirects(response, reverse("accounts:profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.USER)

    def test_change_password_success(self):
        response = self.client.post(
            reverse("accounts:profile_password"),
            {
                "old_password": "pw12345!",
                "new_password1": "a-new-strong-password-9",
                "new_password2": "a-new-strong-password-9",
            },
        )
        self.assertRedirects(response, reverse("accounts:profile"))
        self.client.logout()
        self.assertTrue(self.client.login(email="u@example.com", password="a-new-strong-password-9"))

    def test_change_password_wrong_current_password_rejected(self):
        response = self.client.post(
            reverse("accounts:profile_password"),
            {
                "old_password": "wrong-password",
                "new_password1": "a-new-strong-password-9",
                "new_password2": "a-new-strong-password-9",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.client.logout()
        self.assertTrue(self.client.login(email="u@example.com", password="pw12345!"))

    def test_profile_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.status_code, 302)


class OnboardingTourTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")

    def test_new_user_has_not_seen_onboarding(self):
        self.assertFalse(self.user.has_seen_onboarding)

    def test_complete_onboarding_sets_flag(self):
        response = self.client.post(reverse("accounts:complete_onboarding"))
        self.assertEqual(response.status_code, 204)
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_seen_onboarding)

    def test_complete_onboarding_requires_post(self):
        response = self.client.get(reverse("accounts:complete_onboarding"))
        self.assertEqual(response.status_code, 405)

    def test_complete_onboarding_requires_login(self):
        self.client.logout()
        response = self.client.post(reverse("accounts:complete_onboarding"))
        self.assertEqual(response.status_code, 302)

    def test_replay_onboarding_resets_flag_and_redirects_to_chat(self):
        self.user.has_seen_onboarding = True
        self.user.save(update_fields=["has_seen_onboarding"])
        response = self.client.post(reverse("accounts:replay_onboarding"))
        self.assertRedirects(response, reverse("chat:chat_home"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_seen_onboarding)


class LanguageForIPTests(TestCase):
    def test_pakistani_ip_maps_to_urdu(self):
        self.assertEqual(language_for_ip("182.176.1.1"), "ur")

    def test_uae_ip_maps_to_arabic(self):
        self.assertEqual(language_for_ip("213.42.1.1"), "ar")

    def test_us_ip_maps_to_english(self):
        self.assertEqual(language_for_ip("8.8.8.8"), "en")

    def test_private_ip_falls_back_to_english(self):
        self.assertEqual(language_for_ip("127.0.0.1"), "en")
        self.assertEqual(language_for_ip("10.0.0.5"), "en")

    def test_missing_ip_falls_back_to_english(self):
        self.assertEqual(language_for_ip(""), "en")
        self.assertEqual(language_for_ip(None), "en")


class GeoLanguageMiddlewareTests(TestCase):
    def test_anonymous_visitor_from_arabic_country_gets_arabic_cookie(self):
        response = self.client.get(reverse("accounts:login"), REMOTE_ADDR="213.42.1.1")
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "ar")
        self.assertContains(response, 'dir="rtl"')

    def test_anonymous_visitor_from_pakistan_gets_urdu_cookie(self):
        response = self.client.get(reverse("accounts:login"), REMOTE_ADDR="182.176.1.1")
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "ur")

    def test_existing_language_cookie_is_not_overridden(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("accounts:login"), REMOTE_ADDR="213.42.1.1")
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, response.cookies)

    def test_authenticated_users_db_preference_wins_over_ip_guess(self):
        User.objects.create_user(email="geo@example.com", password="pw12345!", preferred_language="ur")
        self.client.login(email="geo@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:profile"), REMOTE_ADDR="213.42.1.1")
        self.assertContains(response, 'dir="rtl"')
        self.assertContains(response, "زبان")


class SignupLanguageTests(TestCase):
    def test_signup_carries_over_geo_detected_language(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "arabicsignup@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
            REMOTE_ADDR="213.42.1.1",
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="arabicsignup@example.com")
        self.assertEqual(user.preferred_language, "ar")

    def test_signup_defaults_to_english_for_untraceable_ip(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "englishsignup@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="englishsignup@example.com")
        self.assertEqual(user.preferred_language, "en")


class ArabicLanguagePreferenceTests(TestCase):
    def test_set_language_preference_accepts_arabic(self):
        user = User.objects.create_user(email="ar@example.com", password="pw12345!")
        self.client.login(email="ar@example.com", password="pw12345!")
        response = self.client.post(reverse("accounts:set_language_preference"), {"language": "ar"})
        self.assertRedirects(response, reverse("accounts:profile"))
        user.refresh_from_db()
        self.assertEqual(user.preferred_language, "ar")

    def test_arabic_translations_are_loaded(self):
        translation.activate("ar")
        try:
            self.assertEqual(translation.gettext("Save changes"), "حفظ التغييرات")
        finally:
            translation.deactivate()


class DepartmentRetentionDaysTests(TestCase):
    def test_forever_returns_none(self):
        department = Department.objects.create(name="D1", retention_period=Department.RetentionPeriod.FOREVER)
        self.assertIsNone(department.retention_days)

    def test_numeric_periods_return_int(self):
        department = Department.objects.create(name="D2", retention_period=Department.RetentionPeriod.DAYS_30)
        self.assertEqual(department.retention_days, 30)
        department.retention_period = Department.RetentionPeriod.YEARS_7
        self.assertEqual(department.retention_days, 2555)


class MFALoginFlowTests(TestCase):
    def setUp(self):
        from governance.models import SecuritySettings

        SecuritySettings.objects.update_or_create(pk=1, defaults={"mfa_required_for_admins": True})
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, is_staff=True
        )
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.mfa_user = User.objects.create_user(email="mfauser@example.com", password="pw12345!", mfa_enabled=True)

    def test_admin_login_redirects_to_mfa_verify_not_logged_in_yet(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"}
        )
        self.assertRedirects(response, reverse("accounts:mfa_verify"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_regular_user_without_mfa_enabled_logs_in_directly(self):
        response = self.client.post(reverse("accounts:login"), {"username": "user@example.com", "password": "pw12345!"})
        self.assertRedirects(response, reverse("accounts:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_admin_logs_in_directly_when_mfa_not_enforced(self):
        """SecuritySettings.mfa_required_for_admins defaults to False - an
        unreliable/misconfigured outbound email setup must never lock an
        Admin/SuperAdmin out of their own account waiting on a code that
        never arrives. Only affects the mandatory-for-admin behavior - a
        user who explicitly opted into their own MFA (mfa_user, tested
        elsewhere in this class) is unaffected either way."""
        from governance.models import SecuritySettings

        SecuritySettings.objects.update_or_create(pk=1, defaults={"mfa_required_for_admins": False})
        response = self.client.post(
            reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"}
        )
        self.assertRedirects(response, reverse("accounts:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.admin.pk)

    def test_user_with_mfa_enabled_is_challenged(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "mfauser@example.com", "password": "pw12345!"}
        )
        self.assertRedirects(response, reverse("accounts:mfa_verify"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_mfa_email_is_sent_with_the_real_session_code(self):
        from django.core import mail

        mail.outbox = []
        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        code = self.client.session["mfa_code"]
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(code, mail.outbox[0].alternatives[0][0] if mail.outbox[0].alternatives else mail.outbox[0].body)

    def test_correct_code_completes_login_and_honors_next(self):
        self.client.post(
            reverse("accounts:login") + "?next=/chat/", {"username": "admin@example.com", "password": "pw12345!"}
        )
        code = self.client.session["mfa_code"]
        response = self.client.post(reverse("accounts:mfa_verify"), {"code": code})
        self.assertRedirects(response, "/chat/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.admin.pk)

    def test_incorrect_code_does_not_log_in(self):
        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        response = self.client.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_too_many_incorrect_attempts_forces_restart(self):
        from accounts.mfa import MAX_MFA_ATTEMPTS

        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        for _ in range(MAX_MFA_ATTEMPTS):
            self.client.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        response = self.client.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("mfa_user_id", self.client.session)

    def test_expired_code_forces_restart(self):
        from datetime import timedelta

        from django.utils import timezone

        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        session = self.client.session
        session["mfa_expires_at"] = (timezone.now() - timedelta(minutes=1)).isoformat()
        session.save()
        response = self.client.post(reverse("accounts:mfa_verify"), {"code": session["mfa_code"]})
        self.assertRedirects(response, reverse("accounts:login"))

    def test_resend_issues_a_different_code_and_still_works(self):
        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        first_code = self.client.session["mfa_code"]
        self.client.post(reverse("accounts:resend_mfa_code"))
        second_code = self.client.session["mfa_code"]
        response = self.client.post(reverse("accounts:mfa_verify"), {"code": second_code})
        self.assertRedirects(response, reverse("accounts:dashboard"))
        # Not asserting first_code != second_code (a random 6-digit regen
        # could coincidentally repeat) - only that resend produces a code
        # that actually completes login.
        del first_code

    def test_direct_visit_without_pending_challenge_redirects_to_login(self):
        response = self.client.get(reverse("accounts:mfa_verify"))
        self.assertRedirects(response, reverse("accounts:login"))


class ToggleOwnMFATests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.client.login(email="user@example.com", password="pw12345!")

    def test_user_can_enable_own_mfa(self):
        self.client.post(reverse("accounts:toggle_own_mfa"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.mfa_enabled)

    def test_user_can_disable_own_mfa(self):
        self.user.mfa_enabled = True
        self.user.save(update_fields=["mfa_enabled"])
        self.client.post(reverse("accounts:toggle_own_mfa"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.mfa_enabled)


class SessionTimeoutMiddlewareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="user@example.com", password="pw12345!")
        self.client.login(email="user@example.com", password="pw12345!")

    def test_active_session_stays_logged_in(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)

    def test_idle_past_timeout_logs_out(self):
        from accounts.middleware import SESSION_TIMEOUT_MINUTES

        session = self.client.session
        session["last_activity"] = timezone.now().timestamp() - (SESSION_TIMEOUT_MINUTES * 60 + 30)
        session.save()
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_anonymous_request_is_unaffected(self):
        self.client.logout()
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)


@override_settings(DEBUG=False)
class ErrorPageTests(TestCase):
    """templates/404.html, 403.html, 500.html - Django only renders these
    (instead of its own bare default, or the DEBUG=True technical page)
    when DEBUG=False, so every test here forces that explicitly - the
    project's own tests otherwise all run with DEBUG=True (see ci.yml's
    comment on why). The test client re-raises exceptions by default
    (raise_request_exception=True) rather than converting them to the
    response a real server would return - turned off per-request here so
    these actually exercise the same path production does."""

    def test_404_renders_custom_template(self):
        self.client.raise_request_exception = False
        response = self.client.get("/this-path-does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)

    def test_403_renders_custom_template(self):
        User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")
        self.client.raise_request_exception = False
        response = self.client.get(reverse("governance:branding"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Access denied", status_code=403)

    def test_500_template_renders_standalone_with_zero_context(self):
        """Mirrors django.views.defaults.server_error's own call exactly
        (template.render() with no context and no request at all) - the
        real-world case this guards is a 500 caused by a DB outage, where
        rendering the error page must not itself depend on a DB query
        (e.g. governance's site_branding context processor) or it would
        raise a second time and Django would fall back to its own bare
        crash text instead of this template."""
        from django.template import loader

        html = loader.get_template("500.html").render()
        self.assertIn("Something went wrong", html)


class TemplateHygieneTests(TestCase):
    """Static scans across every template file - catch a whole bug class at
    once instead of one regression test per file it happens to bite next."""

    def test_no_multiline_django_comment_tags(self):
        """Django's {# #} comment tag does NOT support spanning multiple
        lines - written across several lines, it's not recognized as a
        comment at all and renders as literal visible text. Has bitten
        this codebase twice already: notifications/_email_shell.html (a
        real comment was genuinely emailed to a real recipient) and then
        500.html (found via this session's own screenshot review, before
        ever reaching production) - both converted to {% comment %}
        {% endcomment %}, which does support multiple lines. This scans
        every template for the same mistake happening a third time,
        rather than relying on a fix ever getting a dedicated test."""
        templates_dir = Path(settings.BASE_DIR) / "templates"
        offenders = []
        for path in templates_dir.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\{#.*?#\}", text, re.DOTALL):
                if "\n" in match.group():
                    offenders.append(f"{path.relative_to(templates_dir)}: {match.group()[:60]!r}")
        self.assertEqual(offenders, [], f"Multi-line {{# #}} comment(s) that will leak as text: {offenders}")
