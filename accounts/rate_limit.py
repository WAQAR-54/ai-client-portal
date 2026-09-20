"""Lightweight cache-backed rate limiting for a handful of sensitive,
unauthenticated endpoints django-axes doesn't cover - it only ever tracks
LOGIN failures, keyed by username (see AXES_* in config/settings.py).
Signup, password-reset requests, and similar abuse-prone endpoints need
their own limiter.

Uses the same cache Django already has configured (Redis in production,
LocMemCache in local dev - see CACHES in config/settings.py), so this
needs no new dependency and shares whatever backend the rest of the app
already relies on (chat/response_cache.py's exact-match cache, for one).

What happens when the cache (Redis) is unavailable is a POLICY per kind of endpoint, not one
blanket rule (before, every limiter silently failed open, so a Redis outage removed the login,
signup and AI-message limits all at once):

  SECURITY_CRITICAL  login, signup, password reset, Google sign-in. Falls back to a per-process
                     in-memory counter with the SAME limit. It is not shared between the 3 Gunicorn
                     workers, so during an outage an attacker can get at most ~3x the limit - bounded,
                     not unlimited - and legitimate users are not locked out.
  EXPENSIVE          the per-minute AI message limit. Same local fallback, so paid provider calls stay
                     restricted (the database-backed plan quotas were never affected by Redis).
  NORMAL             anything else. Fails open: a broken counter must not break the feature.

Nothing here fails CLOSED on an outage: a limiter that cannot count never blocks anyone it has not
counted itself. The outage is logged once a minute (not per request) and readable via limiter_status().
"""

import logging
import threading
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ratelimit"

SECURITY_CRITICAL = "security_critical"
EXPENSIVE = "expensive"
NORMAL = "normal"
_FALLBACK_POLICIES = (SECURITY_CRITICAL, EXPENSIVE)

_LOCAL_MAX_KEYS = 20_000
_LOG_EVERY_SECONDS = 60
_local_lock = threading.Lock()
_local_counters = {}  # cache_key -> [count, window_end_monotonic]
_status = {"degraded_since": None, "last_error": "", "last_logged": 0.0, "fallback_hits": 0}


def limiter_status():
    """Snapshot for diagnostics: is the shared counter currently unavailable, and since when."""
    with _local_lock:
        return dict(_status, local_keys=len(_local_counters))


def _note_cache_failure(key, exc):
    now = time.monotonic()
    with _local_lock:
        if _status["degraded_since"] is None:
            _status["degraded_since"] = time.time()
        _status["last_error"] = type(exc).__name__
        due = now - _status["last_logged"] >= _LOG_EVERY_SECONDS
        if due:
            _status["last_logged"] = now
    if due:
        # No key in the message (it can hold a username or an IP) and no traceback flood.
        logger.warning(
            "Rate-limit cache unavailable (%s); using the local fallback where the policy allows.", type(exc).__name__
        )


def _note_cache_ok():
    if _status["degraded_since"] is not None:
        with _local_lock:
            _status["degraded_since"] = None


def _local_is_limited(cache_key, limit, window_seconds):
    now = time.monotonic()
    with _local_lock:
        _status["fallback_hits"] += 1
        if len(_local_counters) >= _LOCAL_MAX_KEYS:
            for stale in [k for k, (_, end) in _local_counters.items() if end <= now]:
                del _local_counters[stale]
            if len(_local_counters) >= _LOCAL_MAX_KEYS:  # still full: drop the oldest windows, stay bounded
                for oldest in sorted(_local_counters, key=lambda k: _local_counters[k][1])[: _LOCAL_MAX_KEYS // 10]:
                    del _local_counters[oldest]
        entry = _local_counters.get(cache_key)
        if entry is None or entry[1] <= now:
            _local_counters[cache_key] = [1, now + window_seconds]
            return False
        if entry[0] >= limit:
            return True
        entry[0] += 1
        return False


def is_rate_limited(key, *, limit, window_seconds, policy=NORMAL):
    """True if `key` has already been hit `limit` or more times within
    the last `window_seconds` - increments the counter as a side effect,
    so every call (including the one that returns True) counts as a hit.
    `key` should already identify exactly what's being limited (e.g.
    f"signup:{ip}") - this function adds no further scoping of its own.
    `policy` says what to do when the shared counter is unavailable (module docstring)."""
    cache_key = f"{_KEY_PREFIX}:{key}"
    try:
        count = cache.get(cache_key)
        if count is None:
            cache.set(cache_key, 1, timeout=window_seconds)
            _note_cache_ok()
            return False
        if count >= limit:
            _note_cache_ok()
            return True
        # incr() is atomic on both the Redis and LocMemCache backends
        # this app actually runs on - no read-modify-write race between
        # two concurrent requests from the same key.
        cache.incr(cache_key)
        _note_cache_ok()
        return False
    except Exception as exc:  # noqa: BLE001 - any cache failure is handled by policy below
        _note_cache_failure(key, exc)
        if policy in _FALLBACK_POLICIES:
            return _local_is_limited(cache_key, limit, window_seconds)
        return False


def count_failure(key, *, window_seconds, policy=SECURITY_CRITICAL):
    """Record one failure against `key` without deciding anything (see is_over_limit)."""
    cache_key = f"{_KEY_PREFIX}:{key}"
    try:
        try:
            cache.incr(cache_key)
        except ValueError:  # key absent or expired: start a new window
            cache.set(cache_key, 1, timeout=window_seconds)
        _note_cache_ok()
    except Exception as exc:  # noqa: BLE001
        _note_cache_failure(key, exc)
        if policy in _FALLBACK_POLICIES:
            _local_is_limited(cache_key, 10**9, window_seconds)  # count only; the limit is checked on read


def is_over_limit(key, *, limit, policy=SECURITY_CRITICAL):
    """Read-only: has `key` accumulated `limit` or more failures in its window?"""
    cache_key = f"{_KEY_PREFIX}:{key}"
    try:
        count = cache.get(cache_key) or 0
        _note_cache_ok()
        return count >= limit
    except Exception as exc:  # noqa: BLE001
        _note_cache_failure(key, exc)
        if policy in _FALLBACK_POLICIES:
            with _local_lock:
                entry = _local_counters.get(cache_key)
                return bool(entry and entry[1] > time.monotonic() and entry[0] >= limit)
        return False


def client_ip(request):
    """The real visitor's IP, not Nginx's own loopback address - this
    deployment sits behind Cloudflare (orange-cloud DNS) -> Nginx ->
    Gunicorn (see deployment/nginx.conf.example), so plain REMOTE_ADDR as
    Django sees it is always Nginx's address, never the visitor's. Before
    this, EVERY caller of this function (this module's own rate limits,
    accounts/middleware.py's GeoIP language detection, billing/views.py's
    GeoIP region detection) was silently treating every visitor as the
    same one - turning e.g. SIGNUP_RATE_LIMIT into a global cap shared by
    all 200 users instead of a per-visitor one, and making the public
    pricing page auto-detect the same (wrong) region for everyone.

    Prefers CF-Connecting-IP - set by Cloudflare itself at its edge, not
    spoofable by the client, since this app's DNS is Cloudflare-proxied.
    Falls back to X-Forwarded-For's first entry (set by Nginx's own
    proxy_set_header) for any request that reaches Django without going
    through Cloudflare (local dev, or a direct-IP request); REMOTE_ADDR
    is the final fallback for a request with no proxy in front at all."""
    cf_connecting_ip = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    if cf_connecting_ip:
        return cf_connecting_ip
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")
