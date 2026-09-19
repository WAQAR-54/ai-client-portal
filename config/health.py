"""The ONE implementation of "is the database / Redis reachable", shared by
/healthz/, /healthz/deep/ and the governance dashboard's System status
(governance/system_status.py) so they can never disagree about what
"healthy" means.

Both probes return a fixed vocabulary - never an exception message. That
matters because two callers are anonymous, public endpoints: an exception
string can contain a hostname, a Redis URL (which may carry a password), or
a database error such as `password authentication failed for user "x"`.
Failures are logged by exception CLASS NAME only.
"""

import logging
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

# Bounded on purpose: redis-py has no default timeout, so a blackholed host
# would otherwise hang the caller for the OS's TCP timeout - which for
# /healthz/deep/ means a monitoring probe (or a Docker healthcheck) hanging.
REDIS_PROBE_TIMEOUT_SECONDS = 2

_ENGINE_LABELS = {"sqlite": "SQLite", "postgresql": "PostgreSQL"}

HEALTHY = "healthy"
UNAVAILABLE = "unavailable"
NOT_CONFIGURED = "not_configured"


def check_database():
    engine = _ENGINE_LABELS.get(connection.vendor, connection.vendor.title())
    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        logger.warning("Database health probe failed: %s", type(exc).__name__)
        return {"state": UNAVAILABLE, "engine": engine, "latency_ms": None}
    return {"state": HEALTHY, "engine": engine, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


def check_redis(redis_url=None):
    """Three distinct states, never collapsed:
    not_configured - no REDIS_URL at all (the intentional local-dev setup:
                     LocMemCache + Celery eager, see config/settings.py)
    unavailable    - a URL is set but a real PING didn't succeed in time
    healthy        - a real PING round-tripped
    A URL merely being set is never treated as healthy."""
    url = settings.REDIS_URL if redis_url is None else redis_url
    if not url:
        return {"state": NOT_CONFIGURED, "latency_ms": None}

    import redis

    client = None
    started = time.perf_counter()
    try:
        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=REDIS_PROBE_TIMEOUT_SECONDS,
            socket_timeout=REDIS_PROBE_TIMEOUT_SECONDS,
        )
        client.ping()
    except Exception as exc:
        logger.warning("Redis health probe failed: %s", type(exc).__name__)
        return {"state": UNAVAILABLE, "latency_ms": None}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return {"state": HEALTHY, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
