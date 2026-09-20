"""Counters for AI provider calls: how many, how they ended, how long they took.

Stored as plain integer counters in the shared cache (Redis in production), one key per
(day, provider, model, metric), written with an atomic increment when a call ends - no database
writes and no per-call rows. Only slugs, model ids, a fixed vocabulary of outcomes and numbers are
recorded: never a prompt, a reply, a user, a key or an error message.

The cache is not a ledger: keys expire after eight days and a flush loses them, so these are
operational counters (is a provider failing today? is it slow?), not billing data - Message rows
remain the record of what was actually spent.
"""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

TTL_SECONDS = 8 * 24 * 3600
PREFIX = "aimetrics:v1"
COUNTERS = (
    "requests",
    "success",
    "failure",
    "timeout",
    "rate_limited",
    "fallback_success",  # the reply came from a later candidate after an earlier one failed
    "truncated",
    "latency_ms_sum",  # divide by `success` for the average
)


def _day(when=None):
    return (when or timezone.now()).strftime("%Y%m%d")


def _key(day, provider_slug, model_id, metric):
    return f"{PREFIX}:{day}:{provider_slug}:{model_id}:{metric}"


def _bump(provider_slug, model_id, metric, amount=1):
    key = _key(_day(), provider_slug, model_id, metric)
    try:
        try:
            cache.incr(key, amount)
        except ValueError:  # first hit today
            cache.add(key, 0, TTL_SECONDS)
            cache.incr(key, amount)
    except Exception:  # noqa: BLE001 - metrics must never break a chat reply
        logger.debug("ai metrics write skipped", exc_info=False)


def record_success(provider_slug, model_id, latency_ms, *, fallback=False, truncated=False):
    _bump(provider_slug, model_id, "requests")
    _bump(provider_slug, model_id, "success")
    _bump(provider_slug, model_id, "latency_ms_sum", max(0, int(latency_ms)))
    if fallback:
        _bump(provider_slug, model_id, "fallback_success")
    if truncated:
        _bump(provider_slug, model_id, "truncated")


def record_failure(provider_slug, model_id, category):
    """`category` is a providers.errors key (rate_limited / timeout / ...), a fixed vocabulary."""
    _bump(provider_slug, model_id, "requests")
    _bump(provider_slug, model_id, "failure")
    if category == "rate_limited":
        _bump(provider_slug, model_id, "rate_limited")
    elif category == "timeout":
        _bump(provider_slug, model_id, "timeout")


def read(provider_slug, model_id, days=1):
    """{metric: total} for the last `days` days (today included)."""
    now = timezone.now()
    keys = {
        (offset, metric): _key(_day(now - timedelta(days=offset)), provider_slug, model_id, metric)
        for offset in range(days)
        for metric in COUNTERS
    }
    try:
        found = cache.get_many(list(keys.values()))
    except Exception:  # noqa: BLE001
        found = {}
    totals = dict.fromkeys(COUNTERS, 0)
    for (_offset, metric), key in keys.items():
        totals[metric] += int(found.get(key) or 0)
    return totals


def summary(days=1):
    """Per enabled model plus a grand total, for status pages. Reads only keys of models that exist."""
    from providers.models import ProviderModel

    rows, grand = [], dict.fromkeys(COUNTERS, 0)
    for pm in ProviderModel.objects.filter(is_enabled=True).select_related("provider").order_by("provider__slug"):
        totals = read(pm.provider.slug, pm.model_id, days)
        if not totals["requests"]:
            continue
        for metric in COUNTERS:
            grand[metric] += totals[metric]
        rows.append(
            {
                "provider": pm.provider.slug,
                "model": pm.model_id,
                **totals,
                "avg_latency_ms": round(totals["latency_ms_sum"] / totals["success"]) if totals["success"] else None,
            }
        )
    grand["avg_latency_ms"] = round(grand["latency_ms_sum"] / grand["success"]) if grand["success"] else None
    return {"rows": rows, "total": grand}
