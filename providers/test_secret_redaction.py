"""A provider API key must never reach a log file, an exception message or Sentry.

Regression for: chat/providers.py sent the Gemini key as ?key=... and a rate-limited
call (requests' "Max retries exceeded with url: ...?key=<key>") wrote it to app.log."""

import io
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from chat.providers import GeminiProvider, ProviderError
from config.redaction import SecretRedactionFilter, redact_secrets, scrub_event

SECRET = "AQ.Ab8RN6-fake-fake-fake-fake-fake-fake-fake-fake-12345"
URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:streamGenerateContent"
    f"?key={SECRET}&alt=sse"
)


class RedactSecretsTests(SimpleTestCase):
    def test_masks_a_key_in_a_query_string(self):
        out = redact_secrets(f"Max retries exceeded with url: {URL} (Caused by ResponseError)")
        self.assertNotIn(SECRET, out)
        self.assertIn("key=[REDACTED]&alt=sse", out)  # the rest of the URL stays readable

    def test_masks_header_dumps_and_bearer_tokens(self):
        for raw in (
            "{'Authorization': 'Bearer abcdefghijklmnop1234'}",
            "x-goog-api-key: supersecretvalue123",
            "Authorization: Bearer abcdefghijklmnop1234",
            "x-api-key=supersecretvalue123",
        ):
            out = redact_secrets(raw)
            self.assertNotIn("supersecretvalue123", out, raw)
            self.assertNotIn("abcdefghijklmnop1234", out, raw)

    def test_masks_well_known_key_shapes_without_context(self):
        self.assertNotIn("sk-abcdef0123456789abcdef", redact_secrets("bad key sk-abcdef0123456789abcdef given"))
        self.assertNotIn("AIza" + "x" * 35, redact_secrets("key AIza" + "x" * 35 + " rejected"))

    def test_ordinary_text_is_left_alone(self):
        for text in (
            "Rate limit reached, retry after 30 seconds",
            "The key metrics improved and token usage fell",
            "GET /chat/conversations/12/?starter=hello HTTP/1.1",
            "task-force-2026 finished; sk-1 is short",
            "",
        ):
            self.assertEqual(redact_secrets(text), text)
        self.assertEqual(redact_secrets(None), None)
        self.assertEqual(redact_secrets(42), 42)


class ProviderErrorTests(SimpleTestCase):
    def test_provider_error_never_carries_the_key(self):
        error = ProviderError(f"Max retries exceeded with url: {URL}")
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, repr(error))

    def test_a_plain_message_is_unchanged(self):
        self.assertEqual(str(ProviderError("OpenAI is not connected.")), "OpenAI is not connected.")


class GeminiRequestTests(SimpleTestCase):
    def _provider(self):
        row = SimpleNamespace(name="Google Gemini", get_decrypted_key=lambda: SECRET)
        return GeminiProvider(row)

    def test_the_key_is_sent_in_a_header_not_in_the_url(self):
        response = MagicMock()
        response.iter_lines.return_value = []
        with patch("chat.providers._RETRYING_SESSION.post", return_value=response) as post:
            list(self._provider().stream_chat([{"role": "user", "content": "hi"}], "gemini-flash-latest"))
        args, kwargs = post.call_args
        self.assertEqual(kwargs["headers"], {"x-goog-api-key": SECRET})
        self.assertNotIn("key", kwargs["params"])
        self.assertNotIn(SECRET, args[0])

    def test_complete_also_uses_the_header(self):
        response = MagicMock()
        response.json.return_value = {"candidates": []}
        with patch("chat.providers._RETRYING_SESSION.post", return_value=response) as post:
            self._provider().complete([{"role": "user", "content": "hi"}], "gemini-flash-latest")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"], {"x-goog-api-key": SECRET})
        self.assertNotIn("params", kwargs)

    def test_a_failure_whose_text_contains_the_url_does_not_leak_the_key(self):
        boom = requests.exceptions.RetryError(f"Max retries exceeded with url: {URL}")
        with patch("chat.providers._RETRYING_SESSION.post", side_effect=boom):
            with self.assertRaises(ProviderError) as ctx:
                list(self._provider().stream_chat([{"role": "user", "content": "hi"}], "gemini-flash-latest"))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_the_model_listing_adapter_also_uses_the_header(self):
        from providers.adapters.gemini import GeminiAdapter

        response = MagicMock(status_code=200)
        response.json.return_value = {"models": []}
        adapter = GeminiAdapter(provider=None)
        with patch("providers.adapters.gemini.requests.get", return_value=response) as get:
            adapter.fetch_models(SECRET)
            adapter.test_connection(SECRET)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["headers"], {"x-goog-api-key": SECRET})
            self.assertNotIn("key", call.kwargs["params"])


class LogRedactionTests(SimpleTestCase):
    def _logger(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handler.addFilter(SecretRedactionFilter())
        logger = logging.getLogger("test.redaction")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        return logger, stream

    def test_the_whole_exception_chain_is_masked(self):
        """The original library exception is the __cause__ of ProviderError; its raw text
        (with the URL) is printed in the chained traceback."""
        logger, stream = self._logger()
        try:
            try:
                raise requests.exceptions.RetryError(f"Max retries exceeded with url: {URL}")
            except requests.exceptions.RetryError as exc:
                raise RuntimeError("provider call failed") from exc
        except RuntimeError:
            logger.exception("AI provider call failed (%s)", URL)
        output = stream.getvalue()
        self.assertNotIn(SECRET, output)
        self.assertIn("provider call failed", output)  # still a useful log
        self.assertIn("key=[REDACTED]", output)

    def test_the_project_log_handlers_have_the_filter(self):
        from django.conf import settings

        for name in ("console", "file"):
            self.assertIn("redact_secrets", settings.LOGGING["handlers"][name]["filters"])


class SentryScrubTests(SimpleTestCase):
    def test_scrub_event_masks_nested_strings(self):
        event = {
            "exception": {"values": [{"type": "RetryError", "value": f"url: {URL}"}]},
            "breadcrumbs": {"values": [{"message": f"POST {URL}", "data": {"url": URL}}]},
            "tags": {"ai.provider": "gemini"},
            "n": 3,
        }
        cleaned = scrub_event(event)
        self.assertNotIn(SECRET, repr(cleaned))
        self.assertEqual(cleaned["tags"], {"ai.provider": "gemini"})
        self.assertEqual(cleaned["n"], 3)
