"""Data behind the SuperAdmin "Server health" panel of System status (templates/governance/_server_health.html).

Real readings only. On Linux (the production container) the numbers come from /proc, which a container reads from
its HOST, so they describe the server, not just this process: CPU from /proc/stat, memory from /proc/meminfo, load
from /proc/loadavg, uptime from /proc/uptime, disk from statvfs on the application volume. Where a reading cannot be
taken (Windows/macOS development machines have no /proc; a file is unreadable or malformed) that metric is reported
as Unavailable - never as 0 %.

Cost: one small snapshot (four tiny file reads and one statvfs, no subprocess, no network, no directory walk) is kept
in the shared cache for SERVER_HEALTH_CACHE_SECONDS (45 s by default). Every dashboard view and every 60 s refresh
inside that window reuses it, so the number of collections does not grow with the number of viewers. CPU % is the
average utilisation between two consecutive collections (the previous /proc/stat counters are kept in the cache
too); the window is shown. With no usable previous counters it takes one 0.2 s sample instead.

Nothing here reports a path, an environment value, a credential, an address or a container identifier.

Docker: the Django process has no Docker access (no socket is mounted, deliberately), so container state cannot be
read. The service rows are therefore INFERRED from checks System status already runs (database probe, Redis probe,
the recorded outcomes of scheduled tasks) and labelled as such; the panel says "Docker status unavailable".
"""

import logging
import os
import shutil
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

PROC = Path("/proc")  # replaced in tests with a directory of fake files
SNAPSHOT_KEY = "server_health:snapshot"
CPU_PREVIOUS_KEY = "server_health:cpu_previous"
CPU_PREVIOUS_KEPT_SECONDS = 15 * 60  # how long the last /proc/stat counters are kept
CPU_PREVIOUS_MAX_AGE_SECONDS = 10 * 60  # older than this is not "recent" utilisation: take a short sample instead
CPU_QUICK_SAMPLE_SECONDS = 0.2

HEALTHY, WARNING, CRITICAL, UNAVAILABLE = "healthy", "warning", "critical", "unavailable"


def _setting(name, default):
    return getattr(settings, name, default)


def state_for(percent, warn, critical):
    """healthy / warning / critical for a percentage (>= warn is a warning, >= critical is critical)."""
    if percent is None:
        return UNAVAILABLE
    if percent >= critical:
        return CRITICAL
    if percent >= warn:
        return WARNING
    return HEALTHY


def format_bytes(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def format_uptime(seconds):
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


# -- readers: each returns None when the reading cannot be taken -----------------------------------------------------
def _read(name):
    try:
        return (PROC / name).read_text()
    except (OSError, ValueError):
        return None


def _cpu_counters():
    """(busy, total) jiffies since boot from the aggregate 'cpu' line of /proc/stat, or None."""
    text = _read("stat")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            try:
                fields = [int(value) for value in line.split()[1:9]]
            except ValueError:
                return None
            if len(fields) < 4:
                return None
            fields += [0] * (8 - len(fields))
            idle = fields[3] + fields[4]  # idle + iowait
            total = sum(fields)
            return total - idle, total
    return None


def _cpu_from(first, second):
    busy = second[0] - first[0]
    total = second[1] - first[1]
    if total <= 0 or busy < 0:
        return None
    return min(100.0, max(0.0, busy / total * 100))


def read_cpu(now):
    """{"percent", "window_seconds"} or None."""
    counters = _cpu_counters()
    if counters is None:
        return None
    try:
        previous = cache.get(CPU_PREVIOUS_KEY)
        cache.set(CPU_PREVIOUS_KEY, {"at": now, "counters": counters}, CPU_PREVIOUS_KEPT_SECONDS)
    except Exception:  # noqa: BLE001 - the cache being down must not take the panel down with it
        previous = None
    if previous:
        window = now - previous["at"]
        percent = _cpu_from(tuple(previous["counters"]), counters)
        if 1 <= window <= CPU_PREVIOUS_MAX_AGE_SECONDS and percent is not None:
            return {"percent": percent, "window_seconds": int(window)}
    time.sleep(CPU_QUICK_SAMPLE_SECONDS)
    second = _cpu_counters()
    percent = _cpu_from(counters, second) if second else None
    return None if percent is None else {"percent": percent, "window_seconds": CPU_QUICK_SAMPLE_SECONDS}


def read_memory():
    """{"total", "used", "percent"} in bytes from MemTotal/MemAvailable, or None."""
    text = _read("meminfo")
    if not text:
        return None
    values = {}
    for line in text.splitlines():
        key, _sep, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            try:
                values[key] = int(rest.split()[0]) * 1024  # meminfo is in kB
            except (ValueError, IndexError):
                return None
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None or available > total:
        return None
    used = total - available
    return {"total": total, "used": used, "percent": used / total * 100}


def read_disk():
    """{"total", "used", "percent"} in bytes for the application volume, or None."""
    try:
        usage = shutil.disk_usage(settings.BASE_DIR)
    except OSError:
        return None
    if not usage.total:
        return None
    return {"total": usage.total, "used": usage.used, "percent": usage.used / usage.total * 100}


def read_uptime():
    text = _read("uptime")
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def read_load():
    """{"one", "five", "fifteen", "cores"} or None (cores may be None)."""
    text = _read("loadavg")
    try:
        one, five, fifteen = (float(part) for part in text.split()[:3])
    except (AttributeError, ValueError):  # no file, or not three numbers
        return None
    return {"one": one, "five": five, "fifteen": fifteen, "cores": os.cpu_count()}


def collect_snapshot(now=None):
    """One reading of everything. Cheap and read-only; each piece is independent, so one failing reading leaves the
    others intact (and that one Unavailable)."""
    now = time.time() if now is None else now
    snapshot = {"collected_at": now}
    for key, reader in (
        ("cpu", lambda: read_cpu(now)),
        ("memory", read_memory),
        ("disk", read_disk),
        ("uptime", read_uptime),
        ("load", read_load),
    ):
        try:
            snapshot[key] = reader()
        except Exception:  # noqa: BLE001 - a reading that blows up is a reading that is unavailable
            logger.warning("Server health: %s reading failed", key)
            snapshot[key] = None
    return snapshot


def get_snapshot():
    """The cached snapshot, collected again only when it is older than SERVER_HEALTH_CACHE_SECONDS."""
    ttl = int(_setting("SERVER_HEALTH_CACHE_SECONDS", 45))
    try:
        cached = cache.get(SNAPSHOT_KEY)
    except Exception:  # noqa: BLE001
        cached = None
    if cached and time.time() - cached["collected_at"] < ttl:
        return cached
    snapshot = collect_snapshot()
    try:
        cache.set(SNAPSHOT_KEY, snapshot, ttl)
    except Exception:  # noqa: BLE001
        pass
    return snapshot


# -- what the template shows -----------------------------------------------------------------------------------------
def _percent_metric(key, label, reading, warn, critical, detail):
    if reading is None:
        return {"key": key, "label": label, "available": False, "state": UNAVAILABLE, "value": "", "detail": ""}
    percent = reading["percent"]
    return {
        "key": key,
        "label": label,
        "available": True,
        "state": state_for(percent, warn, critical),
        "value": f"{percent:.0f}%",
        "detail": detail(reading),
    }


def build_metrics(snapshot):
    cpu, memory, disk = snapshot["cpu"], snapshot["memory"], snapshot["disk"]
    metrics = [
        _percent_metric(
            "cpu",
            "CPU",
            cpu,
            _setting("SERVER_HEALTH_CPU_WARN_PCT", 70),
            _setting("SERVER_HEALTH_CPU_CRITICAL_PCT", 85),
            lambda r: _cpu_detail(r["window_seconds"]),
        ),
        _percent_metric(
            "memory",
            "Memory",
            memory,
            _setting("SERVER_HEALTH_MEMORY_WARN_PCT", 75),
            _setting("SERVER_HEALTH_MEMORY_CRITICAL_PCT", 90),
            lambda r: f"{format_bytes(r['used'])} / {format_bytes(r['total'])}",
        ),
        _percent_metric(
            "disk",
            "Disk",
            disk,
            _setting("MEDIA_DISK_WARN_PCT", 80),  # the thresholds Server Media and ops_verify already use
            _setting("MEDIA_DISK_CRITICAL_PCT", 90),
            lambda r: f"{format_bytes(r['used'])} / {format_bytes(r['total'])}",
        ),
    ]
    uptime = snapshot["uptime"]
    metrics.append(
        {
            "key": "uptime",
            "label": "Uptime",
            "available": uptime is not None,
            "state": None if uptime is not None else UNAVAILABLE,
            "value": format_uptime(uptime) if uptime is not None else "",
            "detail": "",
        }
    )
    load = snapshot["load"]
    cores = load and load["cores"]
    metrics.append(
        {
            "key": "load",
            "label": "Load",
            "available": load is not None,
            "state": None if load is not None else UNAVAILABLE,
            "value": f"{load['one']:.2f}" if load else "",
            "detail": (
                f"5 min {load['five']:.2f} · 15 min {load['fifteen']:.2f}"
                + (f" · {cores} core{'s' if cores != 1 else ''}" if cores else "")
                if load
                else ""
            ),
        }
    )
    return metrics


def _cpu_detail(window_seconds):
    if window_seconds < 1:
        return f"sampled over {window_seconds:g} s"
    if window_seconds < 90:
        return f"average over the last {int(window_seconds)} s"
    return f"average over the last {round(window_seconds / 60)} min"


def ago(seconds):
    """'42 seconds' / '3 minutes' - how long ago, without the word 'ago' (the template adds the translated one)."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        return f"{seconds // 60} minute{'s' if seconds // 60 != 1 else ''}"
    return f"{seconds // 3600} hour{'s' if seconds // 3600 != 1 else ''}"


def build_services(database, redis_status, jobs):
    """Web / Worker / Beat / PostgreSQL / Redis, inferred ONLY from results System status already computed (so this
    adds no probe): each row says what it was inferred from. Anything the data cannot support is Unavailable."""
    services = [{"key": "web", "label": "Web", "state": HEALTHY, "detail": "answering this request"}]
    services.append(
        {
            "key": "postgres",
            "label": "PostgreSQL",
            "state": HEALTHY if database["state"] == "healthy" else CRITICAL,
            "detail": "database probe",
        }
    )
    redis_state = redis_status["state"]
    services.append(
        {
            "key": "redis",
            "label": "Redis",
            "state": (
                HEALTHY if redis_state == "healthy" else (CRITICAL if redis_state == "unavailable" else UNAVAILABLE)
            ),
            "detail": "not configured" if redis_state == "not_configured" else "Redis probe",
        }
    )
    judged = [row for row in jobs["rows"] if row["enabled"] and row["judged"]]
    if any(row["health"] in ("ok", "running") for row in judged):
        worker = (HEALTHY, "recurring tasks finishing")
    elif any(row["health"] == "failing" for row in judged):
        worker = (WARNING, "recurring tasks are failing")
    else:
        worker = (UNAVAILABLE, "no task outcome recorded yet")
    services.append({"key": "worker", "label": "Worker", "state": worker[0], "detail": worker[1]})
    if jobs["beat_stale"]:
        beat = (CRITICAL, "scheduler stale")
    elif jobs["last_dispatch_age"]:
        beat = (HEALTHY, f"last dispatch {jobs['last_dispatch_age']} ago")
    else:
        beat = (UNAVAILABLE, "nothing dispatched yet")
    services.append({"key": "beat", "label": "Beat", "state": beat[0], "detail": beat[1]})
    return services


def build_panel(database, redis_status, jobs):
    snapshot = get_snapshot()
    collected = snapshot["collected_at"]
    return {
        "metrics": build_metrics(snapshot),
        "services": build_services(database, redis_status, jobs),
        "updated_ago": ago(time.time() - collected),
        "collected_at": collected,
        "docker": {"state": UNAVAILABLE},  # no Docker access from this process, by design (module docstring)
    }
