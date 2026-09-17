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
