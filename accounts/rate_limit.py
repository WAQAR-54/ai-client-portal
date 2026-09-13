"""Lightweight cache-backed rate limiting for a handful of sensitive,
unauthenticated endpoints django-axes doesn't cover - it only ever tracks
LOGIN failures, keyed by username (see AXES_* in config/settings.py).
Signup, password-reset requests, and similar abuse-prone endpoints need
their own limiter.

Uses the same cache Django already has configured (Redis in production,
LocMemCache in local dev - see CACHES in config/settings.py), so this
needs no new dependency and shares whatever backend the rest of the app
already relies on (chat/response_cache.py's exact-match cache, for one).

Fails OPEN (never blocks a request) if the cache backend itself has a
hiccup - same "a broken non-critical dependency degrades, never 500s"
philosophy already used throughout this app (see e.g. billing/access.py,
chat/response_cache.py)."""

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ratelimit"


def is_rate_limited(key, *, limit, window_seconds):
    """True if `key` has already been hit `limit` or more times within
    the last `window_seconds` - increments the counter as a side effect,
    so every call (including the one that returns True) counts as a hit.
    `key` should already identify exactly what's being limited (e.g.
    f"signup:{ip}") - this function adds no further scoping of its own."""
    cache_key = f"{_KEY_PREFIX}:{key}"
    try:
        count = cache.get(cache_key)
        if count is None:
            cache.set(cache_key, 1, timeout=window_seconds)
            return False
        if count >= limit:
            return True
        # incr() is atomic on both the Redis and LocMemCache backends
        # this app actually runs on - no read-modify-write race between
        # two concurrent requests from the same key.
        cache.incr(cache_key)
        return False
    except Exception:
        logger.exception("Rate limit check failed for key=%s; allowing the request.", key)
        return False


def client_ip(request):
    """Same plain REMOTE_ADDR read already used elsewhere in this app
    (see billing/views.py's GeoIP region lookup) - not X-Forwarded-For
    aware, matching that existing convention rather than introducing a
    second, different way to resolve a client's IP."""
    return request.META.get("REMOTE_ADDR", "")
