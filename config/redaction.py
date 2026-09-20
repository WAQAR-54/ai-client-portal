"""Keeps credentials out of logs, exception messages and Sentry events.

Why this exists: chat/providers.py used to send the Gemini API key as a URL
query parameter (?key=...). `requests`/urllib3 put the full URL in their
exception text ("Max retries exceeded with url: /v1beta/...?key=<the key>"),
ProviderError re-raised that text, and logger.exception + Sentry's
capture_exception wrote it out - so a rate-limited request logged the key.

The root cause is fixed at the source (the key now travels in a header, see
chat/providers.py). This module is the second layer: whatever a future
library puts in an exception, a credential-shaped value is masked before it
reaches a log file, the console, or Sentry. It is deliberately narrow - it
only touches things that look like credentials - so ordinary log text is
never altered.
"""

import logging
import re
import traceback

REDACTED = "[REDACTED]"

# ?key=... / &api_key=... / &access_token=... inside a URL or query string.
_QUERY_SECRET = re.compile(r"(?i)([?&](?:key|api[_-]?key|access[_-]?token|token)=)[^&\s'\"<>)]+")
# Authorization: Bearer xxx / x-api-key: xxx / x-goog-api-key: xxx (header dumps, dict reprs).
_HEADER_SECRET = re.compile(
    r"(?i)((?:authorization|x-api-key|x-goog-api-key|api-key)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s\"',}]+"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
# Well-known key shapes, in case they appear with no key=/header context.
_KEY_SHAPES = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|xai-[A-Za-z0-9]{16,})")


def redact_secrets(text):
    """Return `text` with credential-shaped substrings masked. Non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    text = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, text)
    text = _HEADER_SECRET.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    return _KEY_SHAPES.sub(REDACTED, text)


class SecretRedactionFilter(logging.Filter):
    """Masks credentials in the message, its arguments and the full exception
    chain of every record that passes through the handler it is attached to.

    The traceback is rendered here (into record.exc_text) rather than left to
    the formatter, because the formatter only reuses exc_text when it is already
    set - that is what lets this cover chained exceptions ("The above exception
    was the direct cause of...") whose original text still holds the raw URL."""

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_secrets(v) for k, v in record.args.items()}
            else:
                record.args = tuple(redact_secrets(a) for a in record.args)
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_secrets("".join(traceback.format_exception(*record.exc_info)).rstrip("\n"))
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        return True


def scrub_event(value):
    """Recursively mask credentials in a Sentry event (exception values,
    breadcrumbs, log messages, request URLs). Returns the same structure."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: scrub_event(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_event(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_event(v) for v in value)
    return value
