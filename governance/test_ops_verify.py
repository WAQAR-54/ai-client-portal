"""`manage.py ops_verify` runs read-only inside the production container after a
deploy. These tests pin the properties that make that safe: it writes nothing, it
prints no credential, it makes exactly one request per news source, and one failing
section never hides the others."""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from chat import live_intelligence as li
from governance.management.commands import ops_verify

WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE", "REPLACE")


def run(*args, **kwargs):
    out = StringIO()
    call_command("ops_verify", *args, stdout=out, **kwargs)
    return out.getvalue()


@override_settings(LIVE_INTELLIGENCE_ENABLED=True)
class OpsVerifyTests(TestCase):
    def test_reports_the_migrations_and_columns_the_deploy_must_have(self):
        output = run("--skip-feeds")
        self.assertIn("OK migrations: chat.0023_message_generation_started_at_message_is_generating: applied", output)
        self.assertIn("OK migrations: chat.0024_message_live_intel: applied", output)
        self.assertIn("OK schema: chat_message.is_generating: present", output)
        self.assertIn("OK schema: chat_message.live_intel: present", output)
        self.assertRegex(output, r"OK schema: \d+ model columns checked, 0 missing")
        self.assertIn("SUMMARY:", output)

    def test_a_missing_column_is_a_failure_not_a_pass(self):
        with patch.object(ops_verify, "EXPECTED_COLUMNS", {"chat_message": ("no_such_column",)}):
            output = run("--skip-feeds")
        self.assertIn("FAIL schema: chat_message.no_such_column: MISSING", output)

    def test_it_is_strictly_read_only(self):
        with CaptureQueriesContext(connection) as queries:
            run("--skip-feeds")
        writes = [q["sql"][:60] for q in queries if q["sql"].lstrip().upper().startswith(WRITE_VERBS)]
        self.assertEqual(writes, [])

    def test_one_failing_section_does_not_hide_the_rest(self):
        with patch.object(ops_verify.Command, "check_migrations", side_effect=RuntimeError("boom")):
            output = run("--skip-feeds")
        self.assertIn("FAIL migrations: check itself errored: RuntimeError", output)
        self.assertIn("schema:", output)  # later sections still ran
        self.assertIn("SUMMARY:", output)

    def test_it_exits_zero_by_default_and_non_zero_only_with_strict(self):
        with patch.object(ops_verify, "EXPECTED_COLUMNS", {"chat_message": ("no_such_column",)}):
            run("--skip-feeds")  # informational: must not raise
            with self.assertRaises(SystemExit) as raised:
                run("--skip-feeds", "--strict")
        self.assertEqual(raised.exception.code, 1)

    def test_annotations_are_only_printed_when_asked(self):
        self.assertNotIn("::notice", run("--skip-feeds"))
        annotated = run("--skip-feeds", "--annotate")
        self.assertIn("::notice title=ops_verify migrations::", annotated)

    def test_exactly_one_request_per_news_source_and_no_other_fetching(self):
        sources = {source[1] for category in li.CATEGORIES.values() for source in category["sources"]}
        story = {"title": "t", "url": "https://example.com/a", "source": "S", "published": None, "summary": ""}
        with patch.object(li, "_fetch_source", return_value=([story], True)) as fetch:
            with patch.object(li, "_http_get", side_effect=AssertionError("no direct fetching")):
                output = run()
        self.assertEqual(fetch.call_count, len(sources))
        self.assertIn("REACHABLE, 1 usable stories", output)

    def test_an_unreachable_source_is_reported_per_source(self):
        with patch.object(li, "_fetch_source", return_value=([], False)):
            output = run()
        self.assertIn("FAIL feeds:", output)
        self.assertIn("UNREACHABLE", output)

    def test_feeds_are_reported_as_skipped_when_the_feature_is_off(self):
        with override_settings(LIVE_INTELLIGENCE_ENABLED=False):
            with patch.object(li, "_fetch_source", side_effect=AssertionError("must not contact feeds")):
                self.assertIn("SKIP feeds: Live Intelligence is switched off", run())

    def test_no_secret_or_private_data_reaches_the_output(self):
        from accounts.models import User
        from providers.models import Provider

        User.objects.create_user(email="private-person@example.com", password="pw12345!")
        provider = Provider.objects.get(slug="openai")
        provider.set_api_key("sk-this-must-never-be-printed-123456") if hasattr(provider, "set_api_key") else None
        output = run("--skip-feeds")
        for forbidden in ("sk-this-must-never", "private-person@example.com", "pw12345"):
            self.assertNotIn(forbidden, output)

    def test_it_counts_leaked_keys_in_logs_without_printing_them(self):
        secret = "AQ.leaked-value-abcdefghijklmnop123456"
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            (logs / "app.log").write_text(
                f"error url: /v1beta/x?key={secret}&alt=sse\nfine line\nurl: /v1beta/x?key=[REDACTED]&alt=sse\n",
                encoding="utf-8",
            )
            with override_settings(BASE_DIR=Path(tmp)):
                output = run("--skip-feeds")
        self.assertIn("WARN logs: 1 log lines across 1 file(s) contain an un-redacted", output)
        self.assertNotIn(secret, output)

    def test_release_is_reported_from_the_environment(self):
        with override_settings(RELEASE_SHA="abcdef1234567890"):
            self.assertIn("OK release: this process was built from abcdef123456", run("--skip-feeds"))
        with override_settings(RELEASE_SHA=""):
            self.assertIn("WARN release:", run("--skip-feeds"))
