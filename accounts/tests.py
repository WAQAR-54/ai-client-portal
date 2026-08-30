from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

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
