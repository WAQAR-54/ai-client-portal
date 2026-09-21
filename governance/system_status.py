"""Data behind the SuperAdmin "System status" section of the governance
dashboard (templates/governance/_system_status.html).

Everything here is read from things that already exist and are cheap to
read: a SELECT 1, a bounded Redis PING, Provider's stored last-sync fields,
and django_celery_beat's PeriodicTask rows. Nothing calls an AI provider
API or Celery's broker (`inspect()`), so the dashboard can never become as
slow or unreliable as the thing it is reporting on.

Two rules keep this honest:
- A status is only ever derived from real data. Where the data can't
  support a claim (e.g. a task whose outcome record is absent), the claim
  is not made: the row says so instead of showing "healthy".
- Nothing raw from a failure ever reaches the page: not an exception
  message, not a Redis URL (which can carry a password), not a provider's
  free-text error. Failures are reduced to a fixed vocabulary here.
"""

import django
from django.utils import timezone

from chat.utils import age_label
from config.health import check_database, check_redis
from governance import server_health
from providers.errors import describe

# How many of the newest model-routed replies to look through for "last
# activity". Bounded so this dashboard query costs the same at 10 thousand
# replies as at 10 million; a provider absent from the window shows "No
# recent activity" (which the column tooltip spells out), not a wrong date.
ACTIVITY_WINDOW = 5000


def _latest_activity_by_provider():
    """{provider_id: datetime of its newest reply}, from Message rows.
    One query, newest first, so the first time a provider is seen is its
    latest activity."""
    from chat.models import Message

    latest = {}
    newest_first = (
        Message.objects.filter(provider_model_used__isnull=False)
        .order_by("-id")
        .values_list("provider_model_used__provider_id", "created_at")[:ACTIVITY_WINDOW]
    )
    for provider_id, created_at in newest_first:
        latest.setdefault(provider_id, created_at)
    return latest


def check_providers():
    from providers.models import Provider

    providers = list(Provider.objects.filter(is_connected=True).order_by("name"))
    activity = _latest_activity_by_provider() if providers else {}

    rows = []
    for provider in providers:
        if provider.last_sync_status == Provider.SyncStatus.SUCCESS:
            state = "healthy"
        elif provider.last_sync_status == Provider.SyncStatus.FAILED:
            state = "failed"
        else:
            state = "never"
        last_activity = activity.get(provider.id)
        rows.append(
            {
                "name": provider.name,
                "slug": provider.slug,
                "state": state,
                "last_synced_at": provider.last_synced_at,
                "age": age_label(provider.last_synced_at),
                "activity_age": age_label(last_activity),
                "reason": describe(provider.last_sync_error)["label"] if state == "failed" else "",
            }
        )

    return {
        "rows": rows,
        "total": len(rows),
        "healthy": sum(1 for r in rows if r["state"] == "healthy"),
        "attention": sum(1 for r in rows if r["state"] == "failed"),
        "never_synced": sum(1 for r in rows if r["state"] == "never"),
    }


def check_jobs():
    """PeriodicTask.last_run_at is when Beat last *dispatched* a task, not proof it finished. The
    outcome (success / failure / duration / running) comes from governance/task_monitor.py, which
    records Celery's own signals in the shared cache. If that record is absent (cache flushed, task
    never ran since it was introduced) the row says "no outcome recorded" - never "healthy".
    A dispatch older than three intervals marks an interval task, and if EVERY enabled interval task
    is that late, the scheduler itself, as stale."""
    from django_celery_beat.models import PeriodicTask
    from governance import task_monitor

    now = timezone.now()
    rows = []
    for task in PeriodicTask.objects.select_related("interval").order_by("name"):
        if not task.enabled:
            state = "disabled"
        elif task.last_run_at is None:
            state = "never_run"
        else:
            state = "active"
        outcome = task_monitor.summarize(task.task, now=now)
        stale = task_monitor.is_stale(task, now=now)
        if state == "disabled":
            health = "disabled"
        elif outcome["outcome"] == "failing":
            health = "failing"
        elif stale:
            health = "stale"
        elif outcome["running"]:
            health = "running"
        elif outcome["outcome"] == "ok":
            health = "ok"
        else:
            health = "unknown"
        rows.append(
            {
                "name": task.name,
                "task": task.task,
                "state": state,
                "health": health,
                "enabled": task.enabled,
                "last_run_at": task.last_run_at,
                "age": age_label(task.last_run_at),
                "run_count": task.total_run_count,
                "stale": stale,
                "judged": task_monitor.expected_interval_seconds(task) is not None,
                "running": outcome["running"],
                "last_success_age": age_label(outcome["last_success_at"]),
                "last_failure_age": age_label(outcome["last_failure_at"]),
                "failure_kind": outcome["last_failure_kind"],
                "duration_ms": outcome["last_duration_ms"],
                "failures": outcome["failures"],
                "retries": outcome["retries"],
                "recorded": outcome["recorded"],
            }
        )

    dispatched = [r["last_run_at"] for r in rows if r["last_run_at"]]
    last_dispatch = max(dispatched) if dispatched else None
    interval_rows = [r for r in rows if r["enabled"] and r["last_run_at"] and r["judged"]]
    return {
        "rows": rows,
        "total": len(rows),
        "enabled": sum(1 for r in rows if r["enabled"]),
        "disabled": sum(1 for r in rows if not r["enabled"]),
        "never_run": sum(1 for r in rows if r["state"] == "never_run"),
        "failing": sum(1 for r in rows if r["health"] == "failing"),
        "stale": sum(1 for r in rows if r["health"] == "stale"),
        "running": sum(1 for r in rows if r["running"]),
        "outcomes_recorded": sum(1 for r in rows if r["recorded"]),
        "beat_stale": bool(interval_rows) and all(r["stale"] for r in interval_rows),
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
            "Local cache / fallback active",
            "No Redis configuration detected · Celery runs tasks inline",
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
        if jobs["failing"]:
            jobs_card = ("danger", "Failing", f"{jobs['failing']} task(s) failed on their last run", foot)
        elif jobs["beat_stale"]:
            jobs_card = ("danger", "Scheduler stale", "No scheduled task has been dispatched on time", foot)
        elif jobs["stale"]:
            jobs_card = ("muted", "Late", f"{jobs['stale']} task(s) overdue", foot)
        else:
            jobs_card = ("info", "Scheduled", detail, foot)

    def card(key, label, icon, values, probed=True):
        """`probed` = measured on this page load (so a "Checked X ago" line is
        truthful). The jobs card is not a probe - it reports stored history."""
        tone, badge, detail, foot = values
        return {
            "key": key,
            "label": label,
            "icon": icon,
            "tone": tone,
            "badge": badge,
            "detail": detail,
            "foot": foot,
            "probed": probed,
        }

    return [
        card(
            "application",
            "Application",
            "application",
            ("success", "Healthy", "Serving requests", f"Django {django.get_version()}"),
        ),
        card("database", "Database", "database", db_card),
        card("redis", "Redis", "redis", redis_card),
        card("jobs", "Background jobs", "jobs", jobs_card, probed=False),
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
        # Reuses the three results above (no extra probe); the metrics themselves come from a cached snapshot.
        "server_health": server_health.build_panel(database, redis_status, jobs),
    }
