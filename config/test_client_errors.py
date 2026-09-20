"""The browser-error beacon logs and counts allow-listed facts only, and cannot be abused."""

import json

from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from config import client_errors as ce

URL = "/client-errors/"
WRITE_VERBS = ("INSERT", "UPDATE", "DELETE")


class ScrubPathTests(SimpleTestCase):
    def test_ids_and_stream_tokens_are_masked(self):
        token = "kXz3Qw9LmN2pRt8VbYc4HdFs6JgA1eUo"
        path = f"/chat/conversations/12/messages/345/stream/{token}/?model_id=9&secret=abc#frag"
        self.assertEqual(ce.scrub_path(path), "/chat/conversations/{n}/messages/{n}/stream/{t}/")
        self.assertNotIn(token, ce.scrub_path(path))

    def test_junk_is_neutralised(self):
        self.assertEqual(ce.scrub_path(None), "/")
        self.assertEqual(ce.scrub_path("https://evil.example/x"), "/")
        self.assertNotIn("<", ce.scrub_path("/a/<script>alert(1)</script>/b"))
        self.assertLessEqual(len(ce.scrub_path("/" + "/".join(["abcdef"] * 60))), 120)


class BeaconTests(TestCase):
    def setUp(self):
        cache.clear()

    def post(self, payload, raw=None, **extra):
        body = raw if raw is not None else json.dumps(payload)
        return self.client.post(URL, data=body, content_type="application/json", **extra)

    def test_a_valid_report_is_logged_scrubbed_and_counted(self):
        with self.assertLogs("client_errors", level="WARNING") as logs:
            response = self.post(
                {
                    "kind": "htmx_response",
                    "status": 500,
                    "page": "/chat/conversations/7/",
                    "request": "/chat/conversations/7/messages/9/stream/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/?q=hi",
                    "message": "user typed a secret password here",
                    "stack": "at secret.js:1",
                }
            )
        self.assertEqual(response.status_code, 204)
        line = logs.output[0]
        self.assertIn("kind=htmx_response status=500", line)
        self.assertIn("page=/chat/conversations/{n}/", line)
        self.assertIn("request=/chat/conversations/{n}/messages/{n}/stream/{t}/", line)
        for forbidden in ("secret", "password", "AAAAAAAA", "q=hi"):
            self.assertNotIn(forbidden, line)
        self.assertEqual(ce.summary()[("htmx_response", 500)], 1)

    def test_only_allow_listed_kinds_are_accepted(self):
        self.assertEqual(self.post({"kind": "drop_all_tables"}).status_code, 400)
        self.assertEqual(self.post({"status": 500}).status_code, 400)
        self.assertEqual(self.post([1, 2]).status_code, 400)
        self.assertEqual(self.post(None, raw="not json").status_code, 400)

    def test_a_bad_status_is_recorded_as_zero_not_trusted(self):
        for status in ("500; DROP", 99999, -1, True, None):
            self.assertEqual(self.post({"kind": "sse", "status": status}).status_code, 204)
        self.assertEqual(ce.summary(), {("sse", 0): 5})

    def test_an_oversized_body_is_refused(self):
        self.assertEqual(self.post({"kind": "sse", "page": "/" + "a" * 5000}).status_code, 400)

    def test_only_post_is_allowed(self):
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_it_is_rate_limited_per_ip(self):
        codes = [
            self.post({"kind": "sse"}, REMOTE_ADDR="198.51.100.9").status_code
            for _ in range(ce.RATE_LIMIT_PER_MINUTE + 3)
        ]
        self.assertEqual(codes.count(204), ce.RATE_LIMIT_PER_MINUTE)
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.post({"kind": "sse"}, REMOTE_ADDR="198.51.100.10").status_code, 204)

    def test_an_expired_session_can_still_report_and_is_flagged_anonymous(self):
        with self.assertLogs("client_errors", level="WARNING") as logs:
            self.post({"kind": "htmx_response", "status": 401})
        self.assertIn("authenticated=False", logs.output[0])

    def test_a_logged_in_report_is_flagged_without_naming_the_user(self):
        user = User.objects.create_user(email="beacon-user@example.com", password="pw12345!")
        self.client.force_login(user)
        with self.assertLogs("client_errors", level="WARNING") as logs:
            self.post({"kind": "js_error"})
        self.assertIn("authenticated=True", logs.output[0])
        self.assertNotIn("beacon-user", logs.output[0])

    def test_it_writes_nothing_to_the_database(self):
        with CaptureQueriesContext(connection) as queries:
            self.post({"kind": "htmx_send"})
        writes = [q["sql"][:40] for q in queries if q["sql"].lstrip().upper().startswith(WRITE_VERBS)]
        self.assertEqual(writes, [])

    def test_a_cache_outage_does_not_break_the_endpoint(self):
        from unittest.mock import patch

        with patch.object(ce.cache, "incr", side_effect=ConnectionError("down")), patch.object(
            ce.cache, "add", side_effect=ConnectionError("down")
        ):
            self.assertEqual(self.post({"kind": "fetch", "status": 502}).status_code, 204)

    def test_every_page_loads_the_reporter(self):
        response = self.client.get("/accounts/login/")
        self.assertContains(response, "js/client-errors.js")
