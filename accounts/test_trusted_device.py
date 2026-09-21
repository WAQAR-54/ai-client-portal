"""Trusted device: one browser per user skips the e-mail MFA step (accounts/trusted_device.py)."""

from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import trusted_device
from accounts.models import TrustedDevice, User
from notifications.models import Notification

PASSWORD = "pw12345!Strong"
UA_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
UA_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"


class TrustedDeviceTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="mfa.user@corp.io", password=PASSWORD, mfa_enabled=True)

    def browser(self, ua=UA_CHROME):
        return Client(HTTP_USER_AGENT=ua)

    def password_login(self, client):
        return client.post(reverse("accounts:login"), {"username": self.user.email, "password": PASSWORD})

    def full_login(self, client):
        """Password, then the emailed code. Returns the final response."""
        first = self.password_login(client)
        self.assertRedirects(first, reverse("accounts:mfa_verify"), fetch_redirect_response=False)
        code = client.session["mfa_code"]
        return client.post(reverse("accounts:mfa_verify"), {"code": code})

    def logged_in(self, client):
        return "_auth_user_id" in client.session


class RegistrationTests(TrustedDeviceTestCase):
    def test_first_login_requires_mfa_and_is_not_logged_in_yet(self):
        client = self.browser()
        response = self.password_login(client)
        self.assertRedirects(response, reverse("accounts:mfa_verify"), fetch_redirect_response=False)
        self.assertFalse(self.logged_in(client))

    def test_successful_mfa_registers_the_device_and_sets_a_secure_cookie(self):
        client = self.browser()
        response = self.full_login(client)
        self.assertTrue(self.logged_in(client))
        device = TrustedDevice.objects.get(user=self.user)
        self.assertEqual((device.label, device.revoked_at), ("Chrome on Windows", None))
        self.assertGreater(device.expires_at, timezone.now() + timedelta(days=29))
        cookie = response.cookies[trusted_device.COOKIE_NAME]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertGreater(int(cookie["max-age"]), 29 * 86400)

    def test_the_raw_token_is_never_stored(self):
        client = self.browser()
        response = self.full_login(client)
        token = response.cookies[trusted_device.COOKIE_NAME].value
        device = TrustedDevice.objects.get(user=self.user)
        self.assertNotEqual(device.token_hash, token)
        self.assertEqual(device.token_hash, trusted_device._hash(token))
        self.assertEqual(len(device.token_hash), 64)
        stored = " ".join(str(v) for v in TrustedDevice.objects.values_list("token_hash", "label"))
        self.assertNotIn(token, stored)

    def test_a_new_trusted_device_notice_goes_through_the_existing_notification_and_email_shell(self):
        self.full_login(self.browser())
        self.assertTrue(Notification.objects.filter(user=self.user, notification_type="new_trusted_device").exists())
        notice = [m for m in mail.outbox if "New trusted device" in m.subject]
        self.assertEqual(len(notice), 1)
        self.assertIn('data-email-shell="global"', notice[0].alternatives[0][0])


class SkipMfaTests(TrustedDeviceTestCase):
    def setUp(self):
        super().setUp()
        self.trusted = self.browser()
        self.full_login(self.trusted)
        self.trusted.post(reverse("accounts:logout"))

    def test_same_browser_with_a_valid_token_skips_mfa(self):
        response = self.password_login(self.trusted)
        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        self.assertTrue(self.logged_in(self.trusted))
        self.assertIsNotNone(TrustedDevice.objects.get(user=self.user).last_used_at)

    def test_a_different_browser_needs_mfa_even_on_the_same_computer(self):
        other = self.browser(UA_FIREFOX)
        self.assertRedirects(self.password_login(other), reverse("accounts:mfa_verify"), fetch_redirect_response=False)
        self.assertFalse(self.logged_in(other))

    def test_cleared_cookies_or_a_private_window_need_mfa(self):
        self.trusted.cookies.clear()
        self.assertRedirects(
            self.password_login(self.trusted), reverse("accounts:mfa_verify"), fetch_redirect_response=False
        )

    def test_an_invalid_token_needs_mfa(self):
        self.trusted.cookies[trusted_device.COOKIE_NAME] = "not-a-real-token"
        self.assertRedirects(
            self.password_login(self.trusted), reverse("accounts:mfa_verify"), fetch_redirect_response=False
        )

    def test_an_expired_token_needs_mfa(self):
        TrustedDevice.objects.filter(user=self.user).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertRedirects(
            self.password_login(self.trusted), reverse("accounts:mfa_verify"), fetch_redirect_response=False
        )

    def test_a_revoked_token_needs_mfa(self):
        trusted_device.revoke_all(self.user)
        self.assertRedirects(
            self.password_login(self.trusted), reverse("accounts:mfa_verify"), fetch_redirect_response=False
        )

    def test_another_users_token_does_not_skip_mfa_for_this_user(self):
        other = User.objects.create_user(email="someone@corp.io", password=PASSWORD, mfa_enabled=True)
        client = self.browser()
        client.post(reverse("accounts:login"), {"username": other.email, "password": PASSWORD})
        client.post(reverse("accounts:mfa_verify"), {"code": client.session["mfa_code"]})
        client.post(reverse("accounts:logout"))
        response = client.post(reverse("accounts:login"), {"username": self.user.email, "password": PASSWORD})
        self.assertRedirects(response, reverse("accounts:mfa_verify"), fetch_redirect_response=False)

    def test_a_wrong_mfa_code_still_fails(self):
        other = self.browser(UA_FIREFOX)
        self.password_login(other)
        response = other.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.logged_in(other))


class OneActiveDeviceTests(TrustedDeviceTestCase):
    def test_a_new_mfa_login_revokes_the_previous_device_and_its_session(self):
        first = self.browser()
        self.full_login(first)
        second = self.browser(UA_FIREFOX)
        self.full_login(second)
        active = TrustedDevice.objects.filter(user=self.user, revoked_at__isnull=True)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().label, "Firefox on Windows")
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 2)
        # the first browser's session was signed out by the existing single-session rule
        first.get(reverse("accounts:dashboard"))
        self.assertFalse(self.logged_in(first))
        self.assertTrue(self.logged_in(second))
        # ...and its old token no longer skips MFA
        self.assertRedirects(self.password_login(first), reverse("accounts:mfa_verify"), fetch_redirect_response=False)

    def test_the_session_being_created_is_not_invalidated(self):
        client = self.browser()
        self.full_login(client)
        self.assertEqual(client.get(reverse("accounts:dashboard")).status_code, 200)


class SignOutAllTests(TrustedDeviceTestCase):
    def test_sign_out_all_revokes_the_device_and_forces_mfa_next_time(self):
        client = self.browser()
        self.full_login(client)
        response = client.post(reverse("accounts:sign_out_all_sessions"))
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertFalse(self.logged_in(client))
        self.assertFalse(TrustedDevice.objects.filter(user=self.user, revoked_at__isnull=True).exists())
        self.assertRedirects(self.password_login(client), reverse("accounts:mfa_verify"), fetch_redirect_response=False)

    def test_it_only_ever_touches_the_signed_in_users_own_devices(self):
        owner = self.browser()
        self.full_login(owner)
        other_user = User.objects.create_user(email="other@corp.io", password=PASSWORD)
        stranger = Client()
        stranger.force_login(other_user)
        stranger.post(reverse("accounts:sign_out_all_sessions"), {"user_id": self.user.pk})
        self.assertTrue(TrustedDevice.objects.filter(user=self.user, revoked_at__isnull=True).exists())

    def test_anonymous_and_get_requests_are_refused(self):
        self.assertEqual(Client().post(reverse("accounts:sign_out_all_sessions")).status_code, 302)
        client = self.browser()
        self.full_login(client)
        self.assertEqual(client.get(reverse("accounts:sign_out_all_sessions")).status_code, 405)
        self.assertTrue(TrustedDevice.objects.filter(user=self.user, revoked_at__isnull=True).exists())

    def test_the_security_settings_show_the_current_device(self):
        client = self.browser()
        self.full_login(client)
        page = client.get(reverse("accounts:profile"))
        self.assertContains(page, "Current device")
        self.assertContains(page, "Chrome on Windows")
        self.assertContains(page, "Sign out all sessions")
