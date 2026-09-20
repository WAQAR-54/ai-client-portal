"""Browser-side failures (htmx requests, the chat SSE stream, uncaught JavaScript errors) reach the
server as one small, allow-listed beacon - so a stream that silently dies in a user's browser is
visible in the logs and in `manage.py ops_verify`, instead of only in that user's console.

What is accepted is deliberately tiny: an event KIND from a fixed list, an HTTP status, and the URL
PATH of the page and of the failed request. Nothing else is read from the body - no message text,
stack trace, form value, query string or user id - and the path is scrubbed before it is stored: a
number becomes {n} and any long token-like segment becomes {t}, because a chat stream URL carries a
per-message credential (Message.stream_token).

The endpoint takes no authentication (an expired session is one of the things it must report), so it
does nothing but log and count, is rate limited per IP, and caps the body at 2 KB. It changes no
state that any other feature reads.
"""

import json
import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.rate_limit import NORMAL, client_ip, is_rate_limited

logger = logging.getLogger("client_errors")

KINDS = ("htmx_response", "htmx_send", "sse", "js_error", "promise_rejection", "fetch")
MAX_BODY_BYTES = 2048
RATE_LIMIT_PER_MINUTE = 30
TTL_SECONDS = 8 * 24 * 3600
_DIGITS = re.compile(r"^\d+$")


def scrub_path(raw):
    """A URL path with ids and tokens masked, no query or fragment, at most 120 characters."""
    path = str(raw or "").split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return "/"
    segments = []
    for segment in path.split("/")[:12]:
        if _DIGITS.match(segment):
            segments.append("{n}")
        elif len(segment) >= 16:
            segments.append("{t}")
        else:
            segments.append(re.sub(r"[^A-Za-z0-9._~-]", "_", segment)[:40])
    return "/".join(segments)[:120]


def _key(day, kind, status):
    return f"clienterr:v1:{day}:{kind}:{status}"


def _count(kind, status):
    key = _key(timezone.now().strftime("%Y%m%d"), kind, status)
    try:
        try:
            cache.incr(key)
        except ValueError:
            cache.add(key, 0, TTL_SECONDS)
            cache.incr(key)
    except Exception:  # noqa: BLE001 - a counter must never fail a request
        pass


def summary(days=1):
    """{(kind, status): count} for the last `days` days, for status output."""
    now = timezone.now()
    statuses = (0, 400, 401, 403, 404, 429, 500, 502, 503, 504)
    keys = {
        (kind, status): [_key((now - timedelta(days=d)).strftime("%Y%m%d"), kind, status) for d in range(days)]
        for kind in KINDS
        for status in statuses
    }
    flat = [k for group in keys.values() for k in group]
    try:
        found = cache.get_many(flat)
    except Exception:  # noqa: BLE001
        found = {}
    totals = {pair: sum(int(found.get(k) or 0) for k in group) for pair, group in keys.items()}
    return {pair: n for pair, n in totals.items() if n}


@csrf_exempt
@require_POST
def client_error(request):
    if is_rate_limited(
        f"clienterr:{client_ip(request)}", limit=RATE_LIMIT_PER_MINUTE, window_seconds=60, policy=NORMAL
    ):
        return HttpResponse(status=429)
    if len(request.body) > MAX_BODY_BYTES:
        return HttpResponseBadRequest()
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponseBadRequest()
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return HttpResponseBadRequest()
    status = data.get("status")
    status = status if isinstance(status, int) and not isinstance(status, bool) and 0 <= status <= 599 else 0
    kind = data["kind"]
    logger.warning(
        "client_error kind=%s status=%s page=%s request=%s authenticated=%s",
        kind,
        status,
        scrub_path(data.get("page")),
        scrub_path(data.get("request")),
        request.user.is_authenticated,
    )
    _count(kind, status)
    return HttpResponse(status=204)
