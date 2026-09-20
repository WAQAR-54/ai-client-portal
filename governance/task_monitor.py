"""What happened to each background task, recorded from Celery's own signals.

Beat only records that it DISPATCHED a task (PeriodicTask.last_run_at), never whether the worker
finished it. This module fills that gap from real events, with no new table and no second scheduler:

  task_prerun   -> the task is running (remember when it started)
  task_postrun  -> it finished: duration, and success when Celery says SUCCESS
  task_failure  -> it raised: the time and the exception CLASS name (never the message, arguments
                   or traceback - those can hold user data or credentials)
  task_retry    -> it asked to be retried

One small record per task NAME is kept in the shared cache (Redis in production), written once when a
task finishes - two cache operations per run, no database writes. Because the store is a cache the
records can vanish (a Redis flush or restart, or the 30-day expiry); a task with no record is reported
as "no outcome recorded", never as healthy. Counters are read-modify-write and may lose a count when
two workers finish the same task name at the same instant: they are diagnostics, not accounting.
"""

import logging
import threading
import time

from celery.signals import task_failure, task_postrun, task_prerun, task_retry
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

RECORD_TTL = 30 * 24 * 3600
KEY = "taskmon:v1:{name}"
# A running marker older than this is treated as a run that died without reporting (a worker killed
# mid-task never fires postrun). Comfortably above CELERY_TASK_TIME_LIMIT (1800 s).
RUNNING_STALE_SECONDS = 2 * 3600

_started = {}  # task_id -> monotonic start; per worker process
_started_lock = threading.Lock()


def _key(name):
    return KEY.format(name=name)


def get_record(name):
    try:
        return cache.get(_key(name)) or {}
    except Exception:  # noqa: BLE001 - monitoring must never depend on the cache being up
        return {}


def _update(name, mutate):
    try:
        record = cache.get(_key(name)) or {}
        mutate(record)
        cache.set(_key(name), record, RECORD_TTL)
    except Exception:  # noqa: BLE001 - never let bookkeeping break a task
        logger.debug("task monitor could not write its record", exc_info=False)


def _now():
    return timezone.now().isoformat()


def on_prerun(task_id=None, task=None, **_kwargs):
    if task is None:
        return
    with _started_lock:
        _started[task_id] = time.monotonic()
        if len(_started) > 1000:  # bounded: forget the oldest entries of runs that never finished
            for stale in list(_started)[:200]:
                del _started[stale]

    def mutate(record):
        record["running_since"] = _now()
        record["running_id"] = task_id

    _update(task.name, mutate)


def on_postrun(task_id=None, task=None, state=None, retval=None, **_kwargs):
    if task is None:
        return
    with _started_lock:
        started = _started.pop(task_id, None)
    duration_ms = round((time.monotonic() - started) * 1000) if started is not None else None

    def mutate(record):
        if record.get("running_id") == task_id:
            record.pop("running_since", None)
            record.pop("running_id", None)
        record["runs"] = record.get("runs", 0) + 1
        record["last_finished_at"] = _now()
        if duration_ms is not None:
            record["last_duration_ms"] = duration_ms
        if state == "SUCCESS":
            record["last_success_at"] = record["last_finished_at"]

    _update(task.name, mutate)


def on_failure(task_id=None, exception=None, sender=None, **_kwargs):
    name = getattr(sender, "name", None)
    if not name:
        return

    def mutate(record):
        record["failures"] = record.get("failures", 0) + 1
        record["last_failure_at"] = _now()
        record["last_failure_kind"] = type(exception).__name__ if exception is not None else "Unknown"

    _update(name, mutate)


def on_retry(sender=None, **_kwargs):
    name = getattr(sender, "name", None)
    if not name:
        return

    def mutate(record):
        record["retries"] = record.get("retries", 0) + 1
        record["last_retry_at"] = _now()

    _update(name, mutate)


def connect():
    """Idempotent: dispatch_uid keeps a second import from registering a handler twice."""
    task_prerun.connect(on_prerun, dispatch_uid="taskmon.prerun", weak=False)
    task_postrun.connect(on_postrun, dispatch_uid="taskmon.postrun", weak=False)
    task_failure.connect(on_failure, dispatch_uid="taskmon.failure", weak=False)
    task_retry.connect(on_retry, dispatch_uid="taskmon.retry", weak=False)


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
def _parse(value):
    from django.utils.dateparse import parse_datetime

    return parse_datetime(value) if value else None


def summarize(name, now=None):
    """The record for one task, reduced to what a status page may show."""
    record = get_record(name)
    now = now or timezone.now()
    success, failure = _parse(record.get("last_success_at")), _parse(record.get("last_failure_at"))
    running_since = _parse(record.get("running_since"))
    running = bool(running_since and (now - running_since).total_seconds() < RUNNING_STALE_SECONDS)
    if not record:
        outcome = "unknown"
    elif failure and (success is None or failure > success):
        outcome = "failing"
    elif success:
        outcome = "ok"
    else:
        outcome = "unknown"
    return {
        "recorded": bool(record),
        "outcome": outcome,  # ok | failing | unknown
        "running": running,
        "abandoned": bool(running_since and not running),
        "last_success_at": success,
        "last_failure_at": failure,
        "last_failure_kind": record.get("last_failure_kind", ""),
        "last_duration_ms": record.get("last_duration_ms"),
        "runs": record.get("runs", 0),
        "failures": record.get("failures", 0),
        "retries": record.get("retries", 0),
    }


def expected_interval_seconds(periodic_task):
    """How often a task should be dispatched, when that is knowable (interval schedules only)."""
    interval = getattr(periodic_task, "interval", None)
    if interval is None:
        return None
    factor = {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1, "microseconds": 0}.get(interval.period, 0)
    return interval.every * factor or None


def is_stale(periodic_task, now=None):
    """An enabled interval task whose last dispatch is older than three intervals (min. 10 minutes).
    Crontab schedules are not judged: their next run time is not a simple interval."""
    if not periodic_task.enabled or periodic_task.last_run_at is None:
        return False
    expected = expected_interval_seconds(periodic_task)
    if not expected:
        return False
    now = now or timezone.now()
    return (now - periodic_task.last_run_at).total_seconds() > max(3 * expected, 600)
