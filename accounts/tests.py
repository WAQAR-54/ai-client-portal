import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from accounts.geo import country_code_for_ip, language_for_ip
from accounts.google_auth import GoogleSignInError
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
        from django.core.cache import cache

        # The login POST is now rate-limited per-username (see
        # LoginRateLimitTests) - the cache-backed counter otherwise
        # accumulates across every TestCase in this run that logs in as
        # the same address.
        cache.clear()
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


class SqlInjectionSafetyTests(TestCase):
    """Not testing a fix - proving there's nothing to fix. Every query in
    this app goes through the Django ORM (confirmed by grepping the whole
    codebase for .raw()/.extra()/cursor.execute()/RawSQL/migrations.
    RunSQL - none exist anywhere), which always sends user input to the
    database as a bound parameter, never concatenated into the SQL
    string. These tests feed classic injection payloads into the most
    user-input-heavy unauthenticated entry points and assert the app
    behaves exactly as it would for any other bad-but-harmless input:
    no 500, no auth bypass, no data leak - never that the payload
    "gets rejected" by some filter, because there is no filter; the
    payload is just an ordinary string value that happens not to match
    anything."""

    # A `--` comment-out, a UNION-based data-leak attempt, a classic
    # tautology used to bypass a `WHERE` clause, and a stacked
    # DROP TABLE - the four textbook payload shapes.
    PAYLOADS = [
        "' OR '1'='1",
        "'; DROP TABLE accounts_user; --",
        "' UNION SELECT email, password FROM accounts_user --",
        "admin'--",
    ]

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.real_user = User.objects.create_user(email="real@example.com", password="pw12345!")

    def test_login_payloads_never_bypass_authentication_or_500(self):
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                response = self.client.post(reverse("accounts:login"), {"username": payload, "password": payload})
                self.assertEqual(response.status_code, 200)  # re-rendered form, never a crash
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_payload_as_password_for_a_real_email_still_fails(self):
        """The sharpest version of the tautology attack: a real, existing
        email paired with a payload as the PASSWORD - if this ever logged
        someone in, the password check itself would be the injection
        point, not just the username field."""
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                response = self.client.post(
                    reverse("accounts:login"), {"username": "real@example.com", "password": payload}
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("_auth_user_id", self.client.session)
        # The real account must still exist and still log in normally
        # afterward - a stacked DROP TABLE payload, if it had executed,
        # would have taken the whole users table down with it.
        self.assertTrue(User.objects.filter(email="real@example.com").exists())
        self.client.login(email="real@example.com", password="pw12345!")
        self.assertIn("_auth_user_id", self.client.session)

    def test_signup_payloads_never_500_and_never_create_a_row_for_a_malformed_email(self):
        from django.core.cache import cache

        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                cache.clear()  # each attempt would otherwise trip SIGNUP_RATE_LIMIT
                response = self.client.post(
                    reverse("accounts:signup"),
                    {"email": payload, "password1": payload, "password2": payload},
                )
                self.assertEqual(response.status_code, 200)  # invalid email format, form re-rendered
                self.assertFalse(User.objects.filter(email=payload).exists())

    def test_global_search_payloads_never_500(self):
        self.client.login(email="real@example.com", password="pw12345!")
        self.real_user.role = User.Role.ADMIN
        self.real_user.is_staff = True
        self.real_user.save()
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                response = self.client.get(reverse("governance:global_search"), {"q": payload})
                self.assertEqual(response.status_code, 200)
        # The users table must still be intact and queryable afterward.
        self.assertTrue(User.objects.filter(email="real@example.com").exists())


class ClientIpTests(TestCase):
    """accounts/rate_limit.py::client_ip - behind this deployment's real
    proxy chain (Cloudflare -> Nginx -> Gunicorn), plain REMOTE_ADDR is
    always Nginx's own address, never the visitor's. Before this fix,
    every rate limit keyed by client_ip() (signup, password-reset,
    Google sign-in) was silently a GLOBAL cap shared by every visitor
    instead of a per-visitor one - the 6th signup attempt from ANYONE,
    anywhere, would have blocked the 7th person's signup too."""

    def test_prefers_cf_connecting_ip(self):
        from accounts.rate_limit import client_ip

        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP="203.0.113.5")
        self.assertEqual(client_ip(request), "203.0.113.5")

    def test_falls_back_to_first_x_forwarded_for_entry(self):
        from accounts.rate_limit import client_ip

        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.5, 127.0.0.1")
        self.assertEqual(client_ip(request), "203.0.113.5")

    def test_falls_back_to_remote_addr_with_no_proxy_headers(self):
        from accounts.rate_limit import client_ip

        request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.5")
        self.assertEqual(client_ip(request), "203.0.113.5")

    def test_signup_rate_limit_is_scoped_per_real_visitor_not_global(self):
        """The actual bug, end to end: two different visitors (identified
        by CF-Connecting-IP, exactly like production traffic) must each
        get their own signup rate-limit allowance - one visitor
        exhausting theirs must never block the other's."""
        from django.core.cache import cache

        from accounts.views import SIGNUP_RATE_LIMIT

        cache.clear()
        for i in range(SIGNUP_RATE_LIMIT):
            self.client.post(
                reverse("accounts:signup"),
                {
                    "email": f"visitor-a-{i}@example.com",
                    "password1": "a-strong-password-123",
                    "password2": "a-strong-password-123",
                },
                HTTP_CF_CONNECTING_IP="203.0.113.1",
            )
            self.client.logout()
        # Visitor A is now rate-limited...
        blocked_response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "visitor-a-blocked@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
            HTTP_CF_CONNECTING_IP="203.0.113.1",
        )
        self.assertFalse(User.objects.filter(email="visitor-a-blocked@example.com").exists())
        # ...but Visitor B, a completely different real visitor, is not.
        other_response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "visitor-b@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
            HTTP_CF_CONNECTING_IP="203.0.113.2",
        )
        self.assertRedirects(other_response, reverse("accounts:dashboard"))
        self.assertTrue(User.objects.filter(email="visitor-b@example.com").exists())
        del blocked_response


class LoginRateLimitTests(TestCase):
    """django-axes only ever counts FAILED logins - someone who already has
    valid (phished/leaked) credentials could otherwise resubmit the login
    form indefinitely to keep spawning fresh MFA challenges, each with its
    own guess/resend allowance (see accounts/mfa.py), defeating that
    per-challenge cap by restarting the cycle instead of exhausting it."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = User.objects.create_user(email="person@example.com", password="pw12345!")

    def _login(self, password="pw12345!"):
        return self.client.post(reverse("accounts:login"), {"username": "person@example.com", "password": password})

    def test_allows_up_to_the_limit(self):
        from accounts.views import LOGIN_RATE_LIMIT

        for _ in range(LOGIN_RATE_LIMIT):
            response = self._login()
            self.assertRedirects(response, reverse("accounts:dashboard"))
            self.client.logout()

    def test_blocks_once_the_limit_is_exceeded_even_with_the_right_password(self):
        from accounts.views import LOGIN_RATE_LIMIT

        for _ in range(LOGIN_RATE_LIMIT):
            self._login()
            self.client.logout()
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_password_attempts_count_toward_the_same_cap(self):
        from accounts.views import LOGIN_RATE_LIMIT

        for _ in range(LOGIN_RATE_LIMIT):
            self._login(password="wrong-password")
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_limit_is_scoped_per_username_not_global(self):
        from accounts.views import LOGIN_RATE_LIMIT

        User.objects.create_user(email="other@example.com", password="pw12345!")
        for _ in range(LOGIN_RATE_LIMIT):
            self._login()
            self.client.logout()
        response = self.client.post(
            reverse("accounts:login"), {"username": "other@example.com", "password": "pw12345!"}
        )
        self.assertRedirects(response, reverse("accounts:dashboard"))


class LoginAuditTests(TestCase):
    """accounts/signals.py::_log_successful_login - fires from Django's
    own user_logged_in signal, so it catches every real login entry
    point uniformly, not just the plain password form."""

    def setUp(self):
        self.user = User.objects.create_user(email="person@example.com", password="pw12345!")

    def test_successful_login_is_audited(self):
        from governance.models import AuditLog

        self.client.post(reverse("accounts:login"), {"username": "person@example.com", "password": "pw12345!"})
        log = AuditLog.objects.get(action_type="auth.login")
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.target_id, str(self.user.id))

    def test_failed_login_is_not_audited_as_a_login(self):
        from governance.models import AuditLog

        self.client.post(reverse("accounts:login"), {"username": "person@example.com", "password": "wrong"})
        self.assertFalse(AuditLog.objects.filter(action_type="auth.login").exists())


@override_settings(AXES_ENABLED=True)
class AxesLockoutAuditTests(TestCase):
    """accounts/signals.py::_log_axes_lockout, connected via
    connect_axes_signals() in AccountsConfig.ready() - untested before
    this, despite existing since axes was wired up. AXES_ENABLED is
    False by default during `manage.py test` (config/settings.py:
    "test" not in sys.argv) so axes itself doesn't track attempts unless
    explicitly turned back on here."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_lockout_is_audited_for_a_real_account(self):
        from axes.utils import reset

        from governance.models import AuditLog

        user = User.objects.create_user(email="target@example.com", password="pw12345!")
        for _ in range(6):
            self.client.post(reverse("accounts:login"), {"username": "target@example.com", "password": "wrong"})
        logs = AuditLog.objects.filter(action_type="auth.lockout")
        self.assertTrue(logs.exists())
        log = logs.first()
        self.assertIsNone(log.actor)
        self.assertEqual(log.target_type, "User")
        self.assertEqual(log.target_id, str(user.id))
        reset(username="target@example.com")

    def test_lockout_is_audited_for_a_nonexistent_account(self):
        from axes.utils import reset

        from governance.models import AuditLog

        for _ in range(6):
            self.client.post(reverse("accounts:login"), {"username": "nobody-real@example.com", "password": "wrong"})
        logs = AuditLog.objects.filter(action_type="auth.lockout")
        self.assertTrue(logs.exists())
        log = logs.first()
        self.assertIsNone(log.actor)
        self.assertEqual(log.target_id, "None")
        reset(username="nobody-real@example.com")


class SignupTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        # signup_view is now rate-limited (see SignupRateLimitTests) -
        # the cache-backed counter is otherwise shared across every
        # TestCase in this run using the test client's default IP.
        cache.clear()

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

    def test_signup_sends_a_welcome_notification(self):
        """Gap 15 (onboarding) - a self-signed-up user previously got no
        welcome email at all, unlike an admin-created account (see
        governance/views.py::_notify_account_created). Reuses the same
        NotificationType.ACCOUNT_CREATED (both mean "your account is
        ready"), just with self-signup-appropriate copy."""
        from notifications.models import Notification, NotificationType

        self.client.post(
            reverse("accounts:signup"),
            {
                "email": "newperson@example.com",
                "password1": "a-strong-password-123",
                "password2": "a-strong-password-123",
            },
        )
        user = User.objects.get(email="newperson@example.com")
        notification = Notification.objects.get(user=user, notification_type=NotificationType.ACCOUNT_CREATED)
        self.assertIn("Welcome", notification.title)


class SignupRateLimitTests(TestCase):
    """django-axes only ever tracks LOGIN failures - self-service signup
    (unauthenticated, no account needed) has no other protection against
    automated mass account creation from one source without this."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _signup(self, email):
        return self.client.post(
            reverse("accounts:signup"),
            {"email": email, "password1": "a-strong-password-123", "password2": "a-strong-password-123"},
        )

    def test_allows_up_to_the_limit(self):
        from accounts.views import SIGNUP_RATE_LIMIT

        for i in range(SIGNUP_RATE_LIMIT):
            response = self._signup(f"person{i}@example.com")
            self.assertRedirects(response, reverse("accounts:dashboard"))
            self.client.logout()

    def test_blocks_once_the_limit_is_exceeded(self):
        from accounts.views import SIGNUP_RATE_LIMIT

        for i in range(SIGNUP_RATE_LIMIT):
            self._signup(f"person{i}@example.com")
            self.client.logout()
        response = self._signup("onemore@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="onemore@example.com").exists())


class PasswordResetFlowTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
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

    def test_completing_a_reset_writes_an_audit_entry_without_the_password(self):
        """Remaining-audit finding: the in-profile password change was
        audited but the emailed-link reset (the path an attacker with
        mailbox access would use) left no trace. Records who and when -
        never the password itself."""
        from django.core import mail

        from governance.models import AuditLog

        mail.outbox = []
        self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        html_body = mail.outbox[0].alternatives[0][0]
        uidb64, token = self._extract_reset_url(html_body).split("password-reset/confirm/", 1)[1].strip("/").split("/")
        confirm_url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token})

        self.client.post(
            confirm_url, {"new_password1": "brand-new-password-456", "new_password2": "brand-new-password-456"}
        )

        entry = AuditLog.objects.get(action_type="user.password_reset_via_email")
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.target_id, str(self.user.id))
        self.assertNotIn("brand-new-password-456", f"{entry.old_value}{entry.new_value}")

    def test_an_invalid_reset_link_writes_no_audit_entry(self):
        from governance.models import AuditLog

        confirm_url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": "invalid", "token": "bad-token"})
        self.client.post(confirm_url, {"new_password1": "whatever-123456", "new_password2": "whatever-123456"})
        self.assertFalse(AuditLog.objects.filter(action_type="user.password_reset_via_email").exists())

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

    def test_repeated_requests_for_the_same_email_are_rate_limited(self):
        """django-axes doesn't cover this endpoint (it only tracks LOGIN
        failures) - without its own cap, this address's inbox could be
        bombed with reset links indefinitely from a single source."""
        from django.core import mail

        from accounts.views import PASSWORD_RESET_EMAIL_RATE_LIMIT

        mail.outbox = []
        for _ in range(PASSWORD_RESET_EMAIL_RATE_LIMIT):
            self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        self.assertEqual(len(mail.outbox), PASSWORD_RESET_EMAIL_RATE_LIMIT)

        response = self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        self.assertEqual(len(mail.outbox), PASSWORD_RESET_EMAIL_RATE_LIMIT)
        # Same redirect either way - a visibly different response once
        # rate-limited would itself leak information (same reasoning as
        # test_request_does_not_reveal_whether_email_exists above).
        self.assertRedirects(response, reverse("accounts:login"))

    def test_rate_limited_request_shows_the_same_message_as_a_real_one(self):
        from accounts.views import PASSWORD_RESET_EMAIL_RATE_LIMIT

        for _ in range(PASSWORD_RESET_EMAIL_RATE_LIMIT):
            self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email})
        response = self.client.post(reverse("accounts:password_reset_request"), {"email": self.user.email}, follow=True)
        messages_list = list(response.context["messages"])
        self.assertTrue(any("we've sent a link" in str(m) for m in messages_list))


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

    def test_change_password_writes_audit_log_without_the_password_itself(self):
        from governance.models import AuditLog

        self.client.post(
            reverse("accounts:profile_password"),
            {
                "old_password": "pw12345!",
                "new_password1": "a-new-strong-password-9",
                "new_password2": "a-new-strong-password-9",
            },
        )
        log = AuditLog.objects.get(action_type="user.password_change")
        self.assertEqual(log.actor, self.user)
        self.assertNotIn("a-new-strong-password-9", log.old_value + log.new_value)

    def test_failed_password_change_is_not_audited(self):
        from governance.models import AuditLog

        self.client.post(
            reverse("accounts:profile_password"),
            {
                "old_password": "wrong-password",
                "new_password1": "a-new-strong-password-9",
                "new_password2": "a-new-strong-password-9",
            },
        )
        self.assertFalse(AuditLog.objects.filter(action_type="user.password_change").exists())

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


class CountryCodeForIPTests(TestCase):
    def test_pakistani_ip_maps_to_pk(self):
        self.assertEqual(country_code_for_ip("182.176.1.1"), "PK")

    def test_uae_ip_maps_to_ae(self):
        self.assertEqual(country_code_for_ip("213.42.1.1"), "AE")

    def test_private_or_missing_ip_returns_none(self):
        self.assertIsNone(country_code_for_ip("127.0.0.1"))
        self.assertIsNone(country_code_for_ip(""))
        self.assertIsNone(country_code_for_ip(None))


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
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

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
        from django.core.cache import cache
        from governance.models import SecuritySettings

        # Same reason as AuthAndRBACTests.setUp: the login POST is now
        # rate-limited per-username, and this class alone logs in as
        # admin@example.com many times across its test methods.
        cache.clear()
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

    def test_resend_cap_forces_restart_instead_of_unlimited_cycling(self):
        """The real bug this closes: resending used to reset mfa_attempts
        to 0 every time with no cap of its own, so alternating "guess up
        to MAX_MFA_ATTEMPTS times, then resend" indefinitely would never
        trip the attempt limit - only MAX_MFA_RESENDS resends are now
        allowed before a real re-login is required, same as exhausting
        MAX_MFA_ATTEMPTS."""
        from accounts.mfa import MAX_MFA_RESENDS

        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        for _ in range(MAX_MFA_RESENDS):
            response = self.client.post(reverse("accounts:resend_mfa_code"))
            self.assertRedirects(response, reverse("accounts:mfa_verify"))
        response = self.client.post(reverse("accounts:resend_mfa_code"))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("mfa_user_id", self.client.session)

    def test_resend_does_not_reset_the_wrong_guess_counter(self):
        """Resetting mfa_attempts on every resend is intentional (a fresh
        code invalidates old guesses anyway) - what's under test here is
        that this alone can no longer be exploited indefinitely, per
        MAX_MFA_RESENDS above; a single resend still legitimately clears
        prior wrong guesses against the code it just replaced."""
        self.client.post(reverse("accounts:login"), {"username": "admin@example.com", "password": "pw12345!"})
        self.client.post(reverse("accounts:mfa_verify"), {"code": "000000"})
        self.assertEqual(self.client.session["mfa_attempts"], 1)
        self.client.post(reverse("accounts:resend_mfa_code"))
        self.assertEqual(self.client.session["mfa_attempts"], 0)


class GoogleSignInTests(TestCase):
    """accounts/google_auth.py + accounts/views.py::google_signin. The
    actual ID-token signature verification is mocked (a real call would
    hit Google's servers) - everything downstream of a verified payload
    (find-or-create, account linking, MFA gating, suspension, the
    enabled/configured gate, rate limiting) is exercised for real."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _enable(self, **extra):
        from governance.models import SecuritySettings

        SecuritySettings.objects.update_or_create(pk=1, defaults={"google_signin_enabled": True, **extra})

    def _payload(self, email="newgoogle@example.com", sub="google-sub-1", name="Jane Doe"):
        return {
            "sub": sub,
            "email": email,
            "email_verified": True,
            "name": name,
            "picture": "https://example.com/pic.jpg",
        }

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_disabled_by_default_even_with_client_id_configured(self):
        response = self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(response.status_code, 403)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_disabled_without_a_configured_client_id_even_if_toggled_on(self):
        # Explicit empty override rather than relying on the ambient
        # default - local dev's own .env may well have a real client ID
        # set (for actually testing the button), which would otherwise
        # make this assertion false in that environment specifically.
        self._enable()
        response = self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(response.status_code, 403)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_creates_a_new_user_on_first_sign_in(self):
        self._enable()
        with patch("accounts.views.verify_google_credential", return_value=self._payload()):
            response = self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect"], reverse("accounts:dashboard"))
        user = User.objects.get(email="newgoogle@example.com")
        self.assertEqual(user.google_sub, "google-sub-1")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.first_name, "Jane")
        self.assertEqual(user.last_name, "Doe")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_first_sign_in_sends_a_welcome_notification(self):
        """Gap 15 (onboarding) - a first-time Google sign-in creates an
        account exactly as much as the password form does, so it gets the
        same self-signup welcome notice (accounts/views.py::
        notify_self_signup_welcome)."""
        from notifications.models import Notification, NotificationType

        self._enable()
        with patch("accounts.views.verify_google_credential", return_value=self._payload()):
            self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        user = User.objects.get(email="newgoogle@example.com")
        self.assertTrue(
            Notification.objects.filter(user=user, notification_type=NotificationType.ACCOUNT_CREATED).exists()
        )

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_links_an_existing_password_account_by_email_instead_of_duplicating(self):
        self._enable()
        existing = User.objects.create_user(email="linkme@example.com", password="pw12345!")
        with patch(
            "accounts.views.verify_google_credential",
            return_value=self._payload(email="linkme@example.com", sub="sub-2"),
        ):
            self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        existing.refresh_from_db()
        self.assertEqual(existing.google_sub, "sub-2")
        self.assertEqual(User.objects.filter(email="linkme@example.com").count(), 1)
        # Linking must never touch their existing password.
        self.assertTrue(existing.has_usable_password())

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_linking_an_existing_account_does_not_resend_the_welcome_notification(self):
        from notifications.models import Notification, NotificationType

        self._enable()
        existing = User.objects.create_user(email="linkme2@example.com", password="pw12345!")
        with patch(
            "accounts.views.verify_google_credential",
            return_value=self._payload(email="linkme2@example.com", sub="sub-3"),
        ):
            self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertFalse(
            Notification.objects.filter(user=existing, notification_type=NotificationType.ACCOUNT_CREATED).exists()
        )

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_second_sign_in_reuses_the_same_user_by_google_sub(self):
        self._enable()
        with patch(
            "accounts.views.verify_google_credential",
            return_value=self._payload(sub="sub-3", email="repeat@example.com"),
        ):
            self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.client.logout()
        with patch(
            "accounts.views.verify_google_credential",
            return_value=self._payload(sub="sub-3", email="repeat@example.com", name="Repeat User"),
        ):
            self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(User.objects.filter(google_sub="sub-3").count(), 1)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_suspended_account_is_rejected(self):
        self._enable()
        User.objects.create_user(
            email="suspended@example.com", password="pw12345!", google_sub="sub-4", is_active=False
        )
        with patch(
            "accounts.views.verify_google_credential",
            return_value=self._payload(email="suspended@example.com", sub="sub-4"),
        ):
            response = self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_admin_still_goes_through_mfa(self):
        self._enable(mfa_required_for_admins=True)
        User.objects.create_user(
            email="gadmin@example.com",
            password="pw12345!",
            role=User.Role.ADMIN,
            is_staff=True,
            google_sub="sub-5",
        )
        with patch(
            "accounts.views.verify_google_credential",
            return_value=self._payload(email="gadmin@example.com", sub="sub-5"),
        ):
            response = self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(response.json()["redirect"], reverse("accounts:mfa_verify"))
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_invalid_token_returns_error_json_not_a_500(self):
        self._enable()
        with patch("accounts.views.verify_google_credential", side_effect=GoogleSignInError("bad token")):
            response = self.client.post(reverse("accounts:google_signin"), {"credential": "tok"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("bad token", response.json()["error"])

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_button_hidden_on_login_page_when_disabled(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertNotContains(response, "g_id_onload")

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id")
    def test_button_shown_on_login_and_signup_pages_when_enabled(self):
        self._enable()
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, "g_id_onload")
        response = self.client.get(reverse("accounts:signup"))
        self.assertContains(response, "g_id_onload")

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_button_hidden_without_a_configured_client_id_even_if_enabled(self):
        self._enable()
        response = self.client.get(reverse("accounts:login"))
        self.assertNotContains(response, "g_id_onload")


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

    def test_400_renders_custom_template(self):
        """A request with a Host header outside ALLOWED_HOSTS raises
        DisallowedHost (a SuspiciousOperation subclass), which Django turns
        into a 400 via django.views.defaults.bad_request - the one status
        of the four that had no custom template before this change."""
        self.client.raise_request_exception = False
        response = self.client.get("/", HTTP_HOST="not-an-allowed-host.example")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Bad request", status_code=400)


class DebugInProductionStartupGuardTests(TestCase):
    """config/settings.py's ENVIRONMENT+DEBUG guard - this is module-level
    code that runs at settings-import time, so it can't be exercised via
    override_settings (settings are already imported by then). A real
    subprocess proves it actually refuses to start, not just that the
    `if` exists."""

    def _run_manage_check(self, **extra_env):
        import os
        import subprocess
        import sys

        from django.conf import settings

        env = {**os.environ, **{k: str(v) for k, v in extra_env.items()}}
        return subprocess.run(
            [sys.executable, "manage.py", "check"],
            cwd=settings.BASE_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_refuses_to_start_with_debug_true_in_production(self):
        # The repo's own local .env always sets DEBUG=True (and takes
        # precedence over any shell env var per settings.py's own
        # read_env(overwrite=True) - see its comment), so this only needs
        # to add ENVIRONMENT=production to reproduce a real deploy that
        # forgot to flip DEBUG off.
        result = self._run_manage_check(ENVIRONMENT="production")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG=True with ENVIRONMENT=production", result.stderr)

    def test_debug_true_outside_production_is_unaffected(self):
        result = self._run_manage_check(ENVIRONMENT="development")
        self.assertEqual(result.returncode, 0, result.stderr)


@override_settings(ADMINS=[("Test Admin", "admin@example.com")])
class AdminErrorAlertTests(TestCase):
    """governance/error_alerts.py::AsyncAdminEmailHandler + notifications/
    tasks.py::send_admin_error_alert - the async replacement for Django's
    synchronous AdminEmailHandler, wired to the "django.request" logger in
    config/settings.py's LOGGING dict."""

    def test_send_mail_dispatches_the_celery_task_instead_of_sending_directly(self):
        from governance.error_alerts import AsyncAdminEmailHandler

        with patch("notifications.tasks.send_admin_error_alert.delay") as mock_delay:
            AsyncAdminEmailHandler().send_mail("ERROR: boom", "full traceback text")
        mock_delay.assert_called_once_with("ERROR: boom", "full traceback text")

    @override_settings(ADMINS=[])
    def test_send_mail_is_a_no_op_with_no_admins_configured(self):
        from governance.error_alerts import AsyncAdminEmailHandler

        with patch("notifications.tasks.send_admin_error_alert.delay") as mock_delay:
            AsyncAdminEmailHandler().send_mail("ERROR: boom", "full traceback text")
        mock_delay.assert_not_called()

    def test_task_emails_every_configured_admin(self):
        from notifications.models import EmailLog
        from notifications.tasks import send_admin_error_alert

        send_admin_error_alert("ERROR: boom", "full traceback text")
        log = EmailLog.objects.get(recipient="admin@example.com")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.subject, "ERROR: boom")

    @override_settings(DEBUG=False)
    def test_a_real_unhandled_exception_emails_admins_without_showing_the_user_anything_internal(self):
        """End-to-end: a genuine unhandled exception during a real request
        must reach an admin's inbox (via the logging handler above) while
        the user who triggered it only ever sees the branded 500 page -
        never a traceback, a file path, or any other internal detail."""
        from notifications.models import EmailLog

        self.client.raise_request_exception = False
        with patch("accounts.views.DashboardView.get_context_data", side_effect=RuntimeError("boom in dashboard")):
            User.objects.create_user(email="u@example.com", password="pw12345!")
            self.client.login(email="u@example.com", password="pw12345!")
            response = self.client.get(reverse("accounts:dashboard"))

        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Something went wrong", status_code=500)
        self.assertNotIn(b"boom in dashboard", response.content)
        self.assertNotIn(b"Traceback", response.content)

        log = EmailLog.objects.get(recipient="admin@example.com")
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertIn("Internal Server Error", log.subject)


class HealthzTests(TestCase):
    """config/urls.py::healthz - the deploy-time and Docker HEALTHCHECK
    target (deployment/healthcheck.py, .github/workflows/ci.yml's
    post-deploy check). Runs a real database query, unlike the old target
    ("/", any response including a 4xx counted as healthy) which could
    report healthy while the database was completely unreachable."""

    def test_healthz_returns_ok_when_database_is_reachable(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_healthz_returns_503_when_database_query_fails(self):
        from unittest.mock import patch

        with patch("config.health.connection") as mock_connection:
            mock_connection.cursor.side_effect = Exception("connection refused")
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")


class VerifySentryCommandTests(TestCase):
    """accounts/management/commands/verify_sentry.py - lets an operator
    prove Sentry actually receives an event, not just that sentry_sdk.
    init() in config/settings.py looks right on paper. Confirmed live
    against a real Sentry project during this session's own manual run
    (a real "Rate-limited via x-sentry-rate-limits" response came back -
    proof the DSN/host is real and reachable, though worth checking the
    project's quota/plan since a rate-limited event is silently dropped
    on Sentry's own side, not retried)."""

    def test_refuses_to_run_without_sentry_dsn(self):
        from django.core.management import CommandError, call_command
        from django.test import override_settings

        with override_settings(SENTRY_DSN=""):
            with self.assertRaises(CommandError):
                call_command("verify_sentry")

    def test_sends_a_message_event_when_dsn_is_set(self):
        from unittest.mock import patch

        from django.core.management import call_command
        from django.test import override_settings

        with override_settings(SENTRY_DSN="https://fake@fake.ingest.sentry.io/1"):
            with patch("sentry_sdk.capture_message", return_value="fake-event-id") as mock_capture, patch(
                "sentry_sdk.flush"
            ):
                call_command("verify_sentry")
        mock_capture.assert_called_once()
        self.assertIn("verify_sentry deliberate test message", mock_capture.call_args[0][0])

    def test_raise_flag_captures_a_real_exception_instead(self):
        from unittest.mock import patch

        from django.core.management import call_command
        from django.test import override_settings

        with override_settings(SENTRY_DSN="https://fake@fake.ingest.sentry.io/1"):
            with patch("sentry_sdk.capture_exception", return_value="fake-event-id") as mock_capture, patch(
                "sentry_sdk.flush"
            ):
                call_command("verify_sentry", "--raise")
        mock_capture.assert_called_once()
        exc = mock_capture.call_args[0][0]
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("verify_sentry deliberate test error", str(exc))


class HealthzDeepTests(TestCase):
    """config/urls.py::healthz_deep - the slower sibling that also checks
    Redis, kept off the hot healthz() path on purpose. Both endpoints are
    ANONYMOUS, so besides reporting state correctly they must never echo an
    exception message (a host, a Redis URL that may carry a password, a
    database error naming a user)."""

    def test_returns_ok_with_database_and_cache_reachable(self):
        response = self.client.get("/healthz/deep/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "ok")

    def test_returns_503_when_database_query_fails_without_leaking_the_error(self):
        from unittest.mock import patch

        with patch("config.health.connection") as mock_connection:
            mock_connection.vendor = "postgresql"
            mock_connection.cursor.side_effect = Exception('FATAL: password authentication failed for user "portal"')
            response = self.client.get("/healthz/deep/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["database"], "unavailable")
        self.assertNotIn("portal", response.content.decode())

    def test_returns_503_when_redis_is_configured_but_unreachable_without_leaking(self):
        """A REAL refused/timed-out connection, not a mock, with a password in
        the URL - which must not appear in the anonymous response."""
        from django.test import override_settings

        with override_settings(REDIS_URL="redis://:hunter2-secret@127.0.0.1:1/0"):
            response = self.client.get("/healthz/deep/")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["redis"], "unavailable")
        self.assertNotIn("hunter2-secret", response.content.decode())
        self.assertNotIn("127.0.0.1", response.content.decode())

    def test_returns_promptly_when_redis_is_unavailable(self):
        """The whole reason the probe is bounded: a dead Redis must not turn
        a monitoring poll into a hang."""
        import time

        from django.test import override_settings

        from config.health import REDIS_PROBE_TIMEOUT_SECONDS

        started = time.monotonic()
        with override_settings(REDIS_URL="redis://127.0.0.1:1/0"):
            self.client.get("/healthz/deep/")
        self.assertLess(time.monotonic() - started, REDIS_PROBE_TIMEOUT_SECONDS + 3)

    def test_returns_503_when_the_redis_probe_times_out(self):
        from unittest.mock import patch

        from django.test import override_settings
        from redis.exceptions import TimeoutError as RedisTimeout

        with override_settings(REDIS_URL="redis://example.invalid:6379/0"), patch("redis.Redis.from_url") as from_url:
            from_url.return_value.ping.side_effect = RedisTimeout("Timeout reading from socket")
            response = self.client.get("/healthz/deep/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["redis"], "unavailable")

    def test_reports_ok_when_redis_answers_ping(self):
        from unittest.mock import patch

        from django.test import override_settings

        with override_settings(REDIS_URL="redis://example.invalid:6379/0"), patch("redis.Redis.from_url") as from_url:
            from_url.return_value.ping.return_value = True
            response = self.client.get("/healthz/deep/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redis"], "ok")

    def test_reports_redis_not_configured_in_local_dev(self):
        """REDIS_URL is blank locally (LocMemCache, see config/settings.py) -
        a normal, healthy state, and distinct from "unavailable"."""
        from django.test import override_settings

        with override_settings(REDIS_URL=""):
            response = self.client.get("/healthz/deep/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["redis"], "not_configured")

    def test_plain_healthz_does_not_leak_a_database_error_either(self):
        from unittest.mock import patch

        with patch("config.health.connection") as mock_connection:
            mock_connection.vendor = "postgresql"
            mock_connection.cursor.side_effect = Exception('connection to server at "10.0.0.5" failed')
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error", "database": "unavailable"})


class RequestIDTests(TestCase):
    """accounts.middleware.RequestIDMiddleware + RequestIDLogFilter - the
    production-readiness audit's Phase 2A finding: there was no way to trace
    one failing request across a log line and its Sentry event. Verifies the
    id is generated, returned to the client, distinct per request, and that
    a log record emitted mid-request actually carries it (the whole point -
    a filter that's wired up but never actually populated on a real record
    would look done without being done)."""

    def test_response_carries_a_request_id_header(self):
        response = self.client.get("/healthz/")
        self.assertIn("X-Request-ID", response)
        self.assertEqual(len(response["X-Request-ID"]), 16)

    def test_two_requests_get_different_ids(self):
        first = self.client.get("/healthz/")["X-Request-ID"]
        second = self.client.get("/healthz/")["X-Request-ID"]
        self.assertNotEqual(first, second)

    def test_a_log_record_emitted_during_the_request_carries_its_id(self):
        import logging

        from accounts.middleware import RequestIDLogFilter

        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                RequestIDLogFilter().filter(record)
                captured.append(record.request_id)

        test_logger = logging.getLogger("chat.views")
        handler = _Capture()
        test_logger.addHandler(handler)
        try:
            with patch("chat.views.classify_complexity", return_value="default"), patch(
                "chat.views.get_provider"
            ) as mock_get_provider:
                from chat.providers import ProviderError

                mock_get_provider.return_value.stream_chat.side_effect = ProviderError("down")

                from accounts.models import User
                from chat.models import Conversation, Message
                from governance.models import Plan
                from governance.plans import assign_plan
                from providers.models import Provider, ProviderModel

                user = User.objects.create_user(email="rid@example.com", password="pw12345!")
                model = ProviderModel.objects.create(
                    provider=Provider.objects.get(slug="openai"),
                    model_id="test-model",
                    tier=ProviderModel.Tier.DEFAULT,
                    input_price_per_mtok=1,
                    output_price_per_mtok=2,
                    is_enabled=True,
                )
                premium = Plan.objects.get(name="Premium")
                assign_plan(user, premium)
                premium.allowed_provider_models.add(model)
                self.client.login(email="rid@example.com", password="pw12345!")
                conversation = Conversation.objects.create(user=user)
                pending = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT, content="")

                response = self.client.get(
                    reverse(
                        "chat:stream_message",
                        kwargs={
                            "conversation_id": conversation.id,
                            "message_id": pending.id,
                            "token": pending.stream_token,
                        },
                    )
                )
                request_id = response["X-Request-ID"]
                list(response.streaming_content)  # drive the generator so the log call inside it actually runs
        finally:
            test_logger.removeHandler(handler)

        self.assertIn(request_id, captured)


class DocsServingTests(TestCase):
    """config/urls.py's /docs/ route (serve_docs) - the plain-language
    guides in docs/ only lived as files in the repo until this route gave
    them a real, shareable URL. Deliberately public (no login needed) -
    someone deciding whether to use the app, or a teammate without an
    account yet, should be able to read these."""

    def test_bare_docs_redirects_to_hub(self):
        response = self.client.get("/docs/")
        self.assertRedirects(response, "/docs/guides/index.html", fetch_redirect_response=False)

    def test_bare_guides_redirects_to_hub(self):
        response = self.client.get("/docs/guides/")
        self.assertRedirects(response, "/docs/guides/index.html", fetch_redirect_response=False)

    def test_hub_and_every_role_guide_serve_without_login(self):
        for path in (
            "/docs/guides/index.html",
            "/docs/guides/user.html",
            "/docs/guides/manager.html",
            "/docs/guides/admin.html",
            "/docs/guides/superadmin.html",
            "/docs/FEATURE_GUIDE.html",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"{path} did not serve")
            self.assertEqual(response["Content-Type"], "text/html")

    def test_nonexistent_doc_404s_rather_than_crashing(self):
        response = self.client.get("/docs/guides/does-not-exist.html")
        self.assertEqual(response.status_code, 404)


class DashboardAdminSetupChecklistTests(TestCase):
    """DashboardView._admin_setup_checklist (Gap 15 - onboarding): a
    brand-new department Admin otherwise has to discover Users/Billing
    Profile/System Prompt on their own by browsing the nav - this
    surfaces the 3 setup steps directly, and disappears once done."""

    def setUp(self):
        self.department = Department.objects.create(name="Engineering")
        self.admin = User.objects.create_user(
            email="admin@example.com", password="pw12345!", role=User.Role.ADMIN, department=self.department
        )
        self.client.login(email="admin@example.com", password="pw12345!")

    def test_shows_all_three_steps_for_a_fresh_department(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, "Add your team")
        self.assertContains(response, "Set up your billing profile")
        self.assertContains(response, "Customize your system prompt")

    def test_disappears_once_every_step_is_done(self):
        from billing.models import DepartmentBillingProfile
        from governance.models import SystemPromptVersion

        User.objects.create_user(email="member@example.com", password="pw12345!", department=self.department)
        DepartmentBillingProfile.objects.create(department=self.department, company_name="Acme")
        SystemPromptVersion.objects.create(department=self.department, content="Be helpful.", is_active=True)

        response = self.client.get(reverse("accounts:dashboard"))
        self.assertNotContains(response, "Finish setting up your department")

    def test_hidden_for_an_admin_with_no_department(self):
        self.admin.department = None
        self.admin.save(update_fields=["department"])
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertNotContains(response, "Finish setting up your department")

    def test_hidden_for_a_plain_user(self):
        User.objects.create_user(
            email="plain@example.com", password="pw12345!", role=User.Role.USER, department=self.department
        )
        self.client.login(email="plain@example.com", password="pw12345!")
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertNotContains(response, "Finish setting up your department")

    def test_billing_and_system_prompt_steps_hidden_when_department_settings_feature_is_off(self):
        from governance.models import RoleFeatureToggle

        RoleFeatureToggle.objects.update_or_create(
            role=User.Role.ADMIN, feature_key="department_settings", defaults={"is_enabled": False}
        )
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, "Add your team")
        self.assertNotContains(response, "Set up your billing profile")


class DashboardUsageAndPlansTests(TestCase):
    """DashboardView now also shows the user's own plan/usage (reusing
    chat/_usage_widget.html) directly on the post-login landing page,
    rather than only inside the chat page's small header popover. A
    Plans grid was tried here too and explicitly removed per feedback -
    billing:my_plans stays the one place for that."""

    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="pw12345!")
        self.client.login(email="u@example.com", password="pw12345!")

    def test_renders_plan_and_usage_cards(self):
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your plan")
        self.assertContains(response, "Your usage")

    def test_your_plan_card_shows_what_it_includes(self):
        """The same per-plan capability checklist the Plans grid below
        already shows for every plan, reused here for just the one this
        user is actually on - not just the bare plan name/badge."""
        from django.utils.html import escape

        response = self.client.get(reverse("accounts:dashboard"))
        self.assertTrue(response.context["current_plan_capabilities"])
        for cap in response.context["current_plan_capabilities"]:
            self.assertContains(response, escape(cap["label"]))

    def test_your_plan_card_has_no_capability_list_with_no_plan(self):
        from governance.models import UserPlanAssignment

        UserPlanAssignment.objects.filter(user=self.user).delete()
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.context["current_plan_capabilities"], [])

    def test_does_not_crash_for_a_user_with_no_plan_assigned(self):
        from governance.models import UserPlanAssignment

        UserPlanAssignment.objects.filter(user=self.user).delete()
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No plan assigned yet.")

    def test_your_plan_card_shows_cancel_plan_for_a_paid_plan(self):
        """billing.views.cancel_plan/resume_plan, reachable from the
        dashboard's "Your plan" card too, not just billing:my_plans."""
        from governance.models import Plan
        from governance.plans import assign_plan

        paid_plan = Plan.objects.create(name="Growth", is_demo=False)
        assign_plan(self.user, paid_plan)
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, "Cancel plan")
        self.assertContains(response, reverse("billing:cancel_plan"))

    def test_your_plan_card_shows_resume_once_cancelled(self):
        from governance.models import Plan
        from governance.plans import assign_plan

        paid_plan = Plan.objects.create(name="Growth", is_demo=False)
        assign_plan(self.user, paid_plan)
        self.client.post(reverse("billing:cancel_plan"))
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertContains(response, "Resume plan")
        self.assertContains(response, "Cancelled")

    def test_demo_plan_has_no_cancel_button(self):
        """A Demo/trial plan is free (no payment taken) - nothing to
        cancel, per the Refund & Cancellation Policy's own framing."""
        response = self.client.get(reverse("accounts:dashboard"))
        self.assertNotContains(response, "Cancel plan")


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


class ScheduledDatabaseBackupTaskTests(TestCase):
    """accounts/tasks.py::run_scheduled_database_backup - the Celery Beat
    wrapper (seeded by accounts/migrations/0013_...) around `manage.py
    backup_database`. Before this, the recurring backup depended entirely
    on someone having separately configured a VPS crontab entry - easy to
    forget, invisible if it was never actually set up."""

    def test_calls_the_backup_management_command(self):
        from accounts.tasks import run_scheduled_database_backup

        with patch("accounts.tasks.call_command") as mock_call_command:
            run_scheduled_database_backup()
        mock_call_command.assert_called_once_with("backup_database")

    def test_command_error_is_logged_not_raised(self):
        """BACKUP_S3_BUCKET not being set yet is an expected, pre-
        configuration state (the command itself raises CommandError for
        it) - this must never surface as a failed/retried Celery task."""
        from django.core.management.base import CommandError

        from accounts.tasks import run_scheduled_database_backup

        with patch("accounts.tasks.call_command", side_effect=CommandError("BACKUP_S3_BUCKET is not set.")):
            run_scheduled_database_backup()  # must not raise

    def test_runs_cleanly_against_the_real_command_on_sqlite(self):
        """End to end, no mocks: on this test suite's own sqlite database
        (matching local dev), the real command just no-ops with its
        "not PostgreSQL" message - proves the task's plumbing (import,
        call_command wiring) works, not just the mocked call."""
        from accounts.tasks import run_scheduled_database_backup

        run_scheduled_database_backup()  # must not raise
