"""Data behind the SuperAdmin "System status" section of the governance
dashboard (templates/governance/_system_status.html).

Everything here is read from things that already exist and are cheap to
read: a SELECT 1, a bounded Redis PING, Provider's stored last-sync fields,
and django_celery_beat's PeriodicTask rows. Nothing calls an AI provider
API or Celery's broker (`inspect()`), so the dashboard can never become as
slow or unreliable as the thing it is reporting on.

Two rules keep this honest:
- A status is only ever derived from real data. Where the data can't
  support a claim (e.g. whether a scheduled task's last run *succeeded* -
  nothing records that), the claim is not made.
- Nothing raw from a failure ever reaches the page: not an exception
  message, not a Redis URL (which can carry a password), not a provider's
  free-text error. Failures are reduced to a fixed vocabulary here.
"""

import re

import django
from django.utils import timezone
from django.utils.timesince import timesince

from config.health import check_database, check_redis


def age_label(moment):
    """ "18 minutes", "1 week" - the single largest unit, without "ago"
    (the template adds the translated word). Empty for a missing moment."""
    if not moment:
        return ""
    age = timesince(moment).split(",")[0].replace(" ", " ")
    return age if not age.startswith("0 ") else "less than a minute"


_HTTP_STATUS = re.compile(r"(?:error code|status(?: code)?|http)[:\s]+(\d{3})", re.IGNORECASE)


def classify_provider_error(raw_error):
    """A provider's stored last_sync_error is free text from that
    provider's API (already stripped of the API key by sanitize_error, but
    still arbitrary). The dashboard shows a category from this fixed list
    instead - or nothing if the text doesn't clearly match one."""
    text = (raw_error or "").lower()
    if not text:
        return ""
    status = _HTTP_STATUS.search(text)
    code = int(status.group(1)) if status else None

    if code in (401, 403) or any(w in text for w in ("api key", "api_key", "authentication", "unauthorized")):
        return "Authentication error"
    if code == 429 or any(w in text for w in ("rate limit", "rate_limit", "quota")):
        return "Rate limited or quota exceeded"
    if any(w in text for w in ("timed out", "timeout", "connection", "getaddrinfo", "name resolution", "network")):
        return "Network error or timeout"
    if (code is not None and code >= 500) or any(w in text for w in ("overloaded", "server error", "unavailable")):
        return "Provider service error"
    return ""


def check_providers():
    from providers.models import Provider

    rows = []
    for provider in Provider.objects.filter(is_connected=True).order_by("name"):
        if provider.last_sync_status == Provider.SyncStatus.SUCCESS:
            state = "healthy"
        elif provider.last_sync_status == Provider.SyncStatus.FAILED:
            state = "failed"
        else:
            state = "never"
        rows.append(
            {
                "name": provider.name,
                "slug": provider.slug,
                "state": state,
                "last_synced_at": provider.last_synced_at,
                "age": age_label(provider.last_synced_at),
                "reason": classify_provider_error(provider.last_sync_error) if state == "failed" else "",
            }
        )

    return {
        "rows": rows,
        "total": len(rows),
        "healthy": sum(1 for r in rows if r["state"] == "healthy"),
        "attention": sum(1 for r in rows if r["state"] == "failed"),
        "unverified": sum(1 for r in rows if r["state"] == "never"),
    }


def check_jobs():
    """PeriodicTask.last_run_at is when Beat last *dispatched* the task to
    a worker - not proof it succeeded, and nothing in this project records
    a task's outcome (no result table; Celery's results live in Redis by
    task id only). So the only statuses the data supports are:
    disabled / not run yet / active (has been dispatched). "Healthy" and
    "Failed" would be claims this data can't back up, so they don't exist."""
    from django_celery_beat.models import PeriodicTask

    rows = []
    for task in PeriodicTask.objects.order_by("name"):
        if not task.enabled:
            state = "disabled"
        elif task.last_run_at is None:
            state = "never_run"
        else:
            state = "active"
        rows.append(
            {
                "name": task.name,
                "task": task.task,
                "state": state,
                "enabled": task.enabled,
                "last_run_at": task.last_run_at,
                "age": age_label(task.last_run_at),
                "run_count": task.total_run_count,
            }
        )

    dispatched = [r["last_run_at"] for r in rows if r["last_run_at"]]
    last_dispatch = max(dispatched) if dispatched else None
    return {
        "rows": rows,
        "total": len(rows),
        "enabled": sum(1 for r in rows if r["enabled"]),
        "disabled": sum(1 for r in rows if not r["enabled"]),
        "never_run": sum(1 for r in rows if r["state"] == "never_run"),
        "last_dispatch_age": age_label(last_dispatch),
    }


def _summary_cards(database, redis_status, jobs):
    """The four top-row cards, as plain data so the template stays dumb.
    `tone` maps to a badge colour: success / danger / muted / info."""
    engine = database["engine"]
    latency = database["latency_ms"]

    if database["state"] == "healthy":
        db_card = ("success", "Healthy", "Connected", f"{engine} · {latency} ms")
    else:
        db_card = ("danger", "Unavailable", "Connection failed", engine)

    if redis_status["state"] == "healthy":
        redis_card = ("success", "Healthy", "Connected", f"{redis_status['latency_ms']} ms round trip")
    elif redis_status["state"] == "unavailable":
        redis_card = ("danger", "Unavailable", "Connection failed", "A Redis URL is set but did not respond")
    else:
        redis_card = (
            "muted",
            "Not configured",
            "No Redis configuration detected",
            "Using in-process cache; Celery runs tasks inline",
        )

    if jobs["total"] == 0:
        jobs_card = ("muted", "None scheduled", "No scheduled tasks", "")
    else:
        detail = f"{jobs['total']} scheduled"
        if jobs["disabled"]:
            detail += f" · {jobs['disabled']} disabled"
        foot = (
            f"Last dispatched {jobs['last_dispatch_age']} ago"
            if jobs["last_dispatch_age"]
            else "No execution recorded yet"
        )
        jobs_card = ("info", "Scheduled", detail, foot)

    def card(key, label, icon, values):
        tone, badge, detail, foot = values
        return {"key": key, "label": label, "icon": icon, "tone": tone, "badge": badge, "detail": detail, "foot": foot}

    return [
        card(
            "application",
            "Application",
            "application",
            ("success", "Healthy", "Serving requests", f"Django {django.get_version()}"),
        ),
        card("database", "Database", "database", db_card),
        card("redis", "Redis", "redis", redis_card),
        card("jobs", "Background jobs", "jobs", jobs_card),
    ]


def build_system_status():
    checked_at = timezone.now()
    database = check_database()
    redis_status = check_redis()
    providers = check_providers()
    jobs = check_jobs()
    return {
        "checked_at": checked_at,
        "checked_age": age_label(checked_at),
        "cards": _summary_cards(database, redis_status, jobs),
        "database": database,
        "redis": redis_status,
        "providers": providers,
        "jobs": jobs,
    }
