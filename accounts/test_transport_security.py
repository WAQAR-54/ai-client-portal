"""HTTPS behind Cloudflare, secure cookies, the visitor-IP rule, and who hears about a crash.

Regressions for what production actually showed: plain http:// answered 200 (no redirect), the CSRF cookie had
no Secure flag, no HSTS header was sent, ADMINS was empty so a crash emailed nobody, and any client that could
reach the origin could choose its own rate-limit identity with a forged CF-Connecting-IP."""

from unittest import mock

from django.core import mail
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from accounts.rate_limit import client_ip

CF_EDGE = "172.71.10.20"  # inside 172.64.0.0/13, a Cloudflare range
VISITOR = "198.51.100.7"
ATTACKER = "203.0.113.66"
FORGED = "8.8.4.4"


def ip_for(**meta):
    return client_ip(RequestFactory().get("/", **meta))


class ClientIpTrustTests(SimpleTestCase):
    def test_cloudflare_connecting_straight_to_gunicorn_is_trusted(self):
        self.assertEqual(ip_for(REMOTE_ADDR=CF_EDGE, HTTP_CF_CONNECTING_IP=VISITOR), VISITOR)

    def test_a_forged_header_from_a_direct_public_connection_is_ignored(self):
        got = ip_for(
            REMOTE_ADDR=ATTACKER, HTTP_CF_CONNECTING_IP=FORGED, HTTP_X_FORWARDED_FOR=FORGED, HTTP_X_REAL_IP=FORGED
        )
        self.assertEqual(got, ATTACKER)

    def test_through_nginx_a_real_cloudflare_request_uses_the_cloudflare_header(self):
        got = ip_for(REMOTE_ADDR="172.18.0.1", HTTP_X_REAL_IP=CF_EDGE, HTTP_CF_CONNECTING_IP=VISITOR)
        self.assertEqual(got, VISITOR)

    def test_through_nginx_a_request_that_bypassed_cloudflare_cannot_choose_its_identity(self):
        """The attack: hit the origin's Nginx directly and send CF-Connecting-IP to become somebody else
        (or a fresh address every request, resetting every per-IP limit)."""
        got = ip_for(
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_REAL_IP=ATTACKER,  # what Nginx saw: not Cloudflare
            HTTP_CF_CONNECTING_IP=FORGED,
            HTTP_X_FORWARDED_FOR=f"{FORGED}, {ATTACKER}",
        )
        self.assertEqual(got, ATTACKER)

    def test_a_cloudflare_request_with_a_garbage_visitor_header_falls_back_to_the_edge_address(self):
        got = ip_for(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP=CF_EDGE, HTTP_CF_CONNECTING_IP="not-an-ip")
        self.assertEqual(got, CF_EDGE)
        self.assertEqual(ip_for(REMOTE_ADDR=CF_EDGE, HTTP_CF_CONNECTING_IP="<script>"), CF_EDGE)

    def test_a_proxy_that_sends_no_real_ip_keeps_the_previous_behaviour(self):
        """An Nginx config without X-Real-IP must not make every visitor look like the proxy."""
        self.assertEqual(ip_for(REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP=VISITOR), VISITOR)
        self.assertEqual(ip_for(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=f"{VISITOR}, 127.0.0.1"), VISITOR)
        self.assertEqual(ip_for(REMOTE_ADDR="127.0.0.1"), "127.0.0.1")

    def test_ipv6_edge_and_visitor(self):
        got = ip_for(REMOTE_ADDR="::1", HTTP_X_REAL_IP="2606:4700::1111", HTTP_CF_CONNECTING_IP="2001:db8::5")
        self.assertEqual(got, "2001:db8::5")
        self.assertEqual(ip_for(REMOTE_ADDR="2001:db8::9", HTTP_CF_CONNECTING_IP=FORGED), "2001:db8::9")

    def test_no_peer_at_all_is_an_empty_key_not_an_exception(self):
        self.assertEqual(client_ip(RequestFactory().get("/", REMOTE_ADDR="")), "")

    @override_settings(CLOUDFLARE_IP_RANGES=["not-a-range", "10.99.0.0/16"])
    def test_the_range_list_is_configurable_and_tolerates_junk(self):
        self.assertEqual(ip_for(REMOTE_ADDR="10.99.1.1", HTTP_CF_CONNECTING_IP=VISITOR), VISITOR)
        self.assertEqual(ip_for(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP=CF_EDGE, HTTP_CF_CONNECTING_IP=FORGED), CF_EDGE)


def visitor(scheme):
    return {"HTTP_CF_VISITOR": '{"scheme":"%s"}' % scheme}


@override_settings(ENFORCE_HTTPS_VIA_CLOUDFLARE=True, CLOUDFLARE_HSTS_SECONDS=86400)
class HttpsBehindCloudflareTests(TestCase):
    def test_an_http_visitor_is_redirected_to_the_same_url_on_https(self):
        response = self.client.get("/accounts/login/?next=/chat/", **visitor("http"))
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://testserver/accounts/login/?next=/chat/")

    def test_a_post_keeps_its_method_with_a_308(self):
        response = self.client.post("/accounts/login/", {"username": "x"}, **visitor("http"))
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response["Location"], "https://testserver/accounts/login/")

    def test_an_https_visitor_is_not_redirected_and_gets_hsts(self):
        response = self.client.get("/accounts/login/", **visitor("https"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Strict-Transport-Security"], "max-age=86400")
        self.assertNotIn("includeSubDomains", response["Strict-Transport-Security"])
        self.assertNotIn("preload", response["Strict-Transport-Security"])

    def test_requests_that_did_not_come_through_cloudflare_are_untouched(self):
        for extra in (
            {},
            {"HTTP_CF_VISITOR": "garbage"},
            {"HTTP_CF_VISITOR": '{"scheme":"ftp"}'},
            {"HTTP_CF_VISITOR": "x" * 500},
        ):
            response = self.client.get("/accounts/login/", **extra)
            self.assertEqual(response.status_code, 200, extra)
            self.assertNotIn("Strict-Transport-Security", response)

    def test_the_health_endpoints_are_never_redirected(self):
        for path in ("/healthz/", "/healthz/deep/"):
            self.assertNotEqual(self.client.get(path, **visitor("http")).status_code, 301, path)

    def test_there_is_no_redirect_loop_the_target_is_https_and_answers(self):
        first = self.client.get("/accounts/login/", **visitor("http"))
        again = self.client.get("/accounts/login/", **visitor("https"))
        self.assertEqual((first.status_code, again.status_code), (301, 200))

    def test_an_unknown_host_is_rejected_not_redirected_to(self):
        response = self.client.get("/accounts/login/", HTTP_HOST="evil.example", **visitor("http"))
        self.assertEqual(response.status_code, 400)

    @override_settings(ENFORCE_HTTPS_VIA_CLOUDFLARE=False, CLOUDFLARE_HSTS_SECONDS=0)
    def test_when_switched_off_nothing_changes(self):
        response = self.client.get("/accounts/login/", **visitor("http"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Strict-Transport-Security", response)

    @override_settings(CLOUDFLARE_HSTS_SECONDS=0)
    def test_the_redirect_and_hsts_are_independent_switches(self):
        self.assertEqual(self.client.get("/accounts/login/", **visitor("http")).status_code, 301)
        self.assertNotIn("Strict-Transport-Security", self.client.get("/accounts/login/", **visitor("https")))


class SecureCookieTests(TestCase):
    @override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_the_csrf_and_session_cookies_carry_the_secure_flag(self):
        response = self.client.get(reverse("accounts:login"), **visitor("https"))
        self.assertTrue(response.cookies["csrftoken"]["secure"])
        User.objects.create_user(email="cookie@example.com", password="pw12345!")
        self.client.post(reverse("accounts:login"), {"username": "cookie@example.com", "password": "pw12345!"})
        session = self.client.cookies["sessionid"]
        self.assertTrue(session["secure"])
        self.assertTrue(session["httponly"])
        self.assertEqual(session["samesite"], "Lax")

    def test_the_production_compose_file_turns_the_transport_settings_on(self):
        from pathlib import Path

        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
        web = compose[compose.index("\n  web:") : compose.index("\n  worker:")]
        for setting in (
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
            "ENFORCE_HTTPS_VIA_CLOUDFLARE",
            "CLOUDFLARE_HSTS_SECONDS",
        ):
            self.assertIn(f"{setting}: ${{{setting}:-", web, setting)


class CrashAlertRecipientTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_user(
            email="owner-root@example.com", password="pw12345!", role=User.Role.SUPERADMIN
        )
        self.dormant = User.objects.create_user(
            email="dormant-root@example.com", password="pw12345!", role=User.Role.SUPERADMIN, is_active=False
        )
        User.objects.create_user(email="plain@example.com", password="pw12345!")
        User.objects.create_user(email="admin-only@example.com", password="pw12345!", role=User.Role.ADMIN)

    @override_settings(ADMINS=[])
    def test_with_no_admins_every_active_superadmin_is_told(self):
        from governance.error_alerts import alert_recipients

        self.assertEqual(alert_recipients(), ["owner-root@example.com"])

    @override_settings(ADMINS=[("Ops", "ops@example.com")])
    def test_a_configured_admins_list_wins(self):
        from governance.error_alerts import alert_recipients

        self.assertEqual(alert_recipients(), ["ops@example.com"])

    @override_settings(ADMINS=[])
    def test_a_database_error_yields_no_recipients_instead_of_raising(self):
        from governance.error_alerts import alert_recipients

        with mock.patch("accounts.models.User.objects") as manager:
            manager.filter.side_effect = RuntimeError("db down")
            self.assertEqual(alert_recipients(), [])

    @override_settings(ADMINS=[])
    def test_the_alert_task_emails_the_superadmin_and_nobody_else(self):
        from notifications.models import EmailLog
        from notifications.tasks import send_admin_error_alert

        send_admin_error_alert("ERROR: boom", "traceback")
        self.assertEqual(list(EmailLog.objects.values_list("recipient", flat=True)), ["owner-root@example.com"])

    @override_settings(ADMINS=[])
    def test_a_real_crash_reaches_the_superadmin_once(self):
        from django.test import Client

        with mock.patch("config.urls.check_database", side_effect=RuntimeError("boom")):
            Client(raise_request_exception=False).get("/healthz/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner-root@example.com"])
        self.assertTrue(mail.outbox[0].subject.startswith("[Django] "))

    @override_settings(ADMINS=[])
    def test_the_health_probe_503_still_sends_nothing(self):
        with mock.patch("config.health.connection") as connection:
            connection.cursor.side_effect = Exception("connection refused")
            statuses = {self.client.get("/healthz/").status_code for _ in range(3)}
        self.assertEqual(statuses, {503})
        self.assertEqual(mail.outbox, [])

    @override_settings(ADMINS=[])
    def test_with_nobody_to_tell_nothing_is_queued(self):
        User.objects.filter(role=User.Role.SUPERADMIN).delete()
        from governance.error_alerts import AsyncAdminEmailHandler

        with mock.patch("notifications.tasks.send_admin_error_alert.delay") as delay:
            AsyncAdminEmailHandler().send_mail("ERROR: boom", "text")
        delay.assert_not_called()
