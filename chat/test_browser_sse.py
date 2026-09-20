"""Real-browser regression for the SSE reply stream (Chromium via Playwright).

A normal reply used to log console.error("Event") every time: the server ends the
response right after the `done` event, and a browser reports any end of an
EventSource it did not close itself as an error. The page now closes the source
on `done`. Both directions are checked here: a clean finish is silent, and a
stream that really breaks is STILL reported.

Skipped (not failed) when Chromium is not installed or the htmx CDN scripts cannot
be loaded - CI does not install browsers. Runs locally with `manage.py test`.
"""

import os
from unittest import SkipTest
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache

from accounts.models import User
from chat.models import Conversation
from chat.providers import StreamChunk
from chat.tests import _grant_premium_plan
from providers.models import Provider, ProviderModel


class SseConsoleTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Playwright's sync API keeps an asyncio loop alive in this thread, which trips
        # Django's "no ORM calls from an async context" guard. This is the documented
        # way to use both together in a test; it is restored in tearDownClass.
        cls._previous_async_unsafe = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        try:
            from playwright.sync_api import sync_playwright

            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser available here
            cls._restore_async_flag()
            super().tearDownClass()
            raise SkipTest(f"Chromium is not available: {type(exc).__name__}")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        cls._restore_async_flag()
        super().tearDownClass()

    @classmethod
    def _restore_async_flag(cls):
        if cls._previous_async_unsafe is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = cls._previous_async_unsafe

    def setUp(self):
        # The live server thread shares this process's cache with every other test; stale
        # entries (PII rules, rate-limit counters keyed by a reused user id) once produced
        # a stray 400 from post_message in a full-suite run.
        cache.clear()
        self.user = User.objects.create_user(email="sse@example.com", password="pw12345!")
        model = ProviderModel.objects.create(
            provider=Provider.objects.get(slug="openai"),
            model_id="sse-model",
            tier=ProviderModel.Tier.DEFAULT,
            input_price_per_mtok=1,
            output_price_per_mtok=2,
            is_enabled=True,
        )
        _grant_premium_plan(self.user, model)
        self.conversation = Conversation.objects.create(user=self.user)
        self.client.force_login(self.user)
        self.session_id = self.client.cookies[settings.SESSION_COOKIE_NAME].value

    def _open_conversation(self, console, *, break_stream=False):
        context = self._browser.new_context()
        context.add_cookies(
            [{"name": settings.SESSION_COOKIE_NAME, "value": self.session_id, "url": self.live_server_url}]
        )
        page = context.new_page()
        page.on("console", lambda message: console.append((message.type, message.text)))
        page.on(
            "response",
            lambda response: (
                console.append(("http", f"{response.status} {response.request.method} {response.url}"))
                if response.status >= 400
                else None
            ),
        )
        if break_stream:
            page.route("**/stream/**", lambda route: route.abort("connectionreset"))
        page.goto(f"{self.live_server_url}/chat/conversations/{self.conversation.id}/", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        if not page.evaluate("typeof window.htmx !== 'undefined'"):
            context.close()
            raise SkipTest("htmx CDN script did not load")
        return context, page

    def _send(self, page, text):
        page.locator("#composer-textarea").fill(text)
        page.locator("#composer-form").evaluate("form => form.requestSubmit()")

    def _assert_a_clean_reply_leaves_the_console_empty(self, mock_get_provider):
        mock_get_provider.return_value.stream_chat.side_effect = lambda *args, **kwargs: iter(
            [
                StreamChunk(text="Hello "),
                StreamChunk(text="world"),
                StreamChunk(done=True, input_tokens=1, output_tokens=2),
            ]
        )
        console = []
        context, page = self._open_conversation(console)
        try:
            for turn, text in enumerate(("first question", "second question", "third question")):
                self._send(page, text)  # repeated replies on one page, as in real use
                page.wait_for_function(
                    "n => document.querySelectorAll('#messages .msg-assistant').length >= n"
                    " && !document.querySelector('#messages [sse-connect]')",
                    arg=turn + 1,
                    timeout=20000,
                )
            page.wait_for_timeout(500)
            replies = page.locator("#messages .msg-assistant .msg-content").all_inner_texts()
        finally:
            context.close()
        self.assertEqual(len(replies), 3)
        self.assertTrue(all("Hello world" in reply for reply in replies), replies)
        self.assertEqual([entry for entry in console if entry[0] == "error"], [])

    def _assert_a_stream_that_really_breaks_is_still_reported(self):
        """The fix must not hide genuine SSE failures."""
        console = []
        context, page = self._open_conversation(console, break_stream=True)
        try:
            self._send(page, "this stream will be cut off")
            page.wait_for_timeout(4000)
        finally:
            context.close()
        self.assertTrue(
            [entry for entry in console if entry[0] == "error"], "a broken stream produced no console error"
        )

    @patch("chat.views.classify_complexity", return_value=ProviderModel.Tier.DEFAULT)
    @patch("chat.views.get_provider")
    def test_sse_console_reporting(self, mock_get_provider, _classify):
        """Both directions in one test method: a live-server test case flushes the
        migration-seeded providers/plans after each method, and the fixtures need them."""
        self._assert_a_clean_reply_leaves_the_console_empty(mock_get_provider)
        self._assert_a_stream_that_really_breaks_is_still_reported()
