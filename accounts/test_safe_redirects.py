"""A posted `next` must never send the browser off-site (open redirect)."""

import re
from pathlib import Path

from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User
from accounts.redirects import safe_next_url
from notifications.models import Notification

ROOT = Path(__file__).resolve().parent.parent
BACKSLASH = chr(92)
EVIL = (
    "https://evil.example/login",
    "http://evil.example",
    "//evil.example/x",
    "///evil.example",
    BACKSLASH * 2 + "evil.example",  # browsers read \host as //host
    "/" + BACKSLASH + "evil.example",
    "javascript:alert(1)",
    "data:text/html,x",
    "https:evil.example",
)


class SafeNextUrlTests(SimpleTestCase):
    def _post(self, value, secure=False):
        request = RequestFactory().post("/x/", {"next": value}, secure=secure)
        return safe_next_url(request, "fallback:name")

    def test_a_same_site_path_is_followed(self):
        for ok in ("/chat/", "/billing/invoices/3/?tab=a#top", "/"):
            self.assertEqual(self._post(ok), ok)

    def test_every_off_site_or_script_url_falls_back_to_the_default(self):
        for bad in EVIL:
            self.assertEqual(self._post(bad), "fallback:name", bad)

    def test_a_missing_or_blank_value_uses_the_default(self):
        self.assertEqual(self._post(""), "fallback:name")
        self.assertEqual(self._post("   "), "fallback:name")

    def test_the_same_host_absolute_url_is_allowed_and_another_host_is_not(self):
        self.assertEqual(self._post("http://testserver/chat/"), "http://testserver/chat/")
        self.assertEqual(self._post("http://other.example/chat/"), "fallback:name")

    def test_over_https_a_plain_http_url_is_refused(self):
        self.assertEqual(self._post("http://testserver/chat/", secure=True), "fallback:name")
        self.assertEqual(self._post("https://testserver/chat/", secure=True), "https://testserver/chat/")


class NotificationRedirectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="redir@example.com", password="pw12345!")
        self.client.force_login(self.user)
        self.notification = Notification.objects.create(
            user=self.user, notification_type="plan_change", title="t", body="b"
        )

    def _endpoints(self):
        return (
            reverse("notifications:mark_read", args=[self.notification.id]),
            reverse("notifications:mark_all_read"),
            reverse("notifications:delete_notifications"),
        )

    def test_none_of_the_views_redirect_off_site(self):
        for url in self._endpoints():
            for bad in EVIL:
                response = self.client.post(url, {"next": bad})
                self.assertEqual(response.status_code, 302, url)
                self.assertEqual(response["Location"], reverse("notifications:list"), (url, bad))

    def test_a_same_site_next_still_works(self):
        for url in self._endpoints():
            self.assertEqual(self.client.post(url, {"next": "/chat/"})["Location"], "/chat/", url)


class NoRawNextRedirectTests(SimpleTestCase):
    def test_no_view_redirects_straight_to_a_posted_next(self):
        """Guard against the pattern coming back: redirect(request.POST.get("next") ...)."""
        pattern = re.compile(r"""(?:redirect|HX-Redirect"\]\s*=)\s*\(?\s*request\.(?:POST|GET)\.get\(\s*["']next["']""")
        offenders = []
        for path in ROOT.rglob("views.py"):
            if "venv" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            offenders += [f"{path.relative_to(ROOT)}:{m.start()}" for m in pattern.finditer(text)]
            offenders += [
                str(path.relative_to(ROOT))
                for m in re.finditer(r"""=\s*request\.POST\.get\(\s*["']next["']\s*\)\s*or""", text)
            ]
        self.assertEqual(offenders, [], "use accounts.redirects.safe_next_url")
