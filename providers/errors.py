"""The one place a provider failure is turned into something a person may see.

A provider's raw error text is arbitrary output from a third party (and from
whatever SDK wrapped it): it can echo a key fragment, request headers, a
response body or a URL. sanitize_error() strips the exact API key, but that
is defence in depth, not a guarantee - so no view, template, admin page or
JSON response renders the raw text. They render describe() instead: a
category from a fixed list plus a short fixed message. Raw text goes to the
server log only (services.py), never into a page.

Backwards compatible without a data migration: Provider.last_sync_error now
STORES the safe label, but rows written before this change still hold raw
text - describe() classifies those on the way out, so they are never shown
verbatim either.
"""

import re

AUTHENTICATION = "authentication"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
INVALID_RESPONSE = "invalid_response"
CONFIGURATION = "configuration"
UNKNOWN = "unknown"

# key -> (label, short safe message). Labels are what gets stored.
CATEGORIES = {
    AUTHENTICATION: (
        "Authentication error",
        "The provider rejected the credentials. Reconnect it with a valid API key.",
    ),
    RATE_LIMITED: ("Rate limited", "The provider is limiting requests, or the account's quota is used up."),
    TIMEOUT: ("Timeout", "The provider took too long to respond."),
    UNAVAILABLE: ("Provider unavailable", "The provider could not be reached or reported a service problem."),
    INVALID_RESPONSE: ("Invalid response", "The provider returned a response the app could not read."),
    CONFIGURATION: ("Configuration error", "The provider is not fully set up."),
    UNKNOWN: ("Unknown provider error", "The sync failed for an unrecognised reason. Details are in the server logs."),
}

_LABEL_TO_KEY = {label.lower(): key for key, (label, _message) in CATEGORIES.items()}
_HTTP_STATUS = re.compile(r"(?:error code|status(?: code)?|http)[:\s]+(\d{3})", re.IGNORECASE)


def classify(raw_error):
    """Category key for raw error text. Deliberately conservative: it only
    recognises clear signals and otherwise says UNKNOWN rather than guess."""
    text = (raw_error or "").lower().strip()
    if not text:
        return UNKNOWN
    match = _HTTP_STATUS.search(text)
    code = int(match.group(1)) if match else None

    if "not connected" in text or "not configured" in text or "unknown provider adapter" in text:
        return CONFIGURATION
    if code in (401, 403) or any(
        w in text for w in ("api key", "api_key", "api-key", "authentication", "unauthorized")
    ):
        return AUTHENTICATION
    if code == 429 or any(w in text for w in ("rate limit", "rate_limit", "quota")):
        return RATE_LIMITED
    if code in (408, 504) or any(w in text for w in ("timed out", "timeout")):
        return TIMEOUT
    if any(w in text for w in ("jsondecode", "json decode", "invalid json", "unexpected response", "malformed")):
        return INVALID_RESPONSE
    if (code is not None and code >= 500) or any(
        w in text for w in ("connection", "getaddrinfo", "name resolution", "network", "overloaded", "unavailable")
    ):
        return UNAVAILABLE
    return UNKNOWN


def describe(stored_or_raw):
    """{"key", "label", "message"} for anything found in
    Provider.last_sync_error: an already-safe stored label, or (legacy rows,
    or a fresh result) raw text. Never returns the raw text itself."""
    text = (stored_or_raw or "").strip()
    key = _LABEL_TO_KEY.get(text.lower()) or classify(text)
    label, message = CATEGORIES[key]
    return {"key": key, "label": label, "message": message}
