"""Read-only operational self-check, meant to be run INSIDE the production
container (see .github/workflows/ci.yml, "Post-deploy verification").

    docker compose exec -T web python manage.py ops_verify [--annotate]

Everything here is read-only: it reads migration state, database catalog views,
file sizes and row COUNTS, makes one GET per news source, and does a Redis
round trip on a single throw-away key that expires by itself after 15 seconds.
It never writes to the database, never deletes anything, never flushes a cache,
and never prints a credential, a user's data, or a file name.

Output is one line per finding: "<STATUS> <section>: <detail>" where STATUS is
OK / WARN / FAIL / SKIP. It always exits 0 (it is informational, and must never
block or roll back a deploy); pass --strict to exit 1 when anything FAILs.
--annotate also prints each line as a GitHub Actions ::notice/::warning/::error
annotation, which is how the result becomes readable from the run's checks.
"""

import os
import re
import shutil
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Sum
from django.utils import timezone

# Sections reported together as one annotation (see _write_annotations).
ANNOTATION_GROUPS = {
    "release": "platform",
    "settings": "platform",
    "migrations": "database",
    "schema": "database",
    "database": "database",
    "redis": "services",
    "celery": "services",
    "beat": "services",
    "providers": "providers",
    "ai": "providers",
    "capacity": "services",
    "frontend": "services",
    "feeds": "feeds",
    "disk": "storage",
    "logs": "storage",
    "retention": "retention",
    "backups": "retention",
}
SEVERITY = {"OK": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
EXPECTED_CHAT_MIGRATIONS = ("0023_message_generation_started_at_message_is_generating", "0024_message_live_intel")
# docker-compose.yml's fallback when the server .env sets no POSTGRES_PASSWORD (a public value in the repository).
COMPOSE_DEFAULT_DB_PASSWORD = "changeme-local-only"
EXPECTED_COLUMNS = {"chat_message": ("is_generating", "generation_started_at", "live_intel")}
# A credential-looking value after key= in a log line (see config/redaction.py).
_LEAKED_KEY = re.compile(r"[?&](?:key|api_key|access_token|token)=(?!\[REDACTED\])[^&\s'\"<>)]{16,}", re.IGNORECASE)


def _dir_stats(path, limit=200000):
    """(file_count, total_bytes) - counts only, never names. Bounded walk."""
    count = size = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            if count >= limit:
                return count, size
    return count, size


def _human(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024


class Command(BaseCommand):
    help = "Read-only production self-check: migrations, schema, Redis, Celery, feeds, disk, logs, database."

    def add_arguments(self, parser):
        parser.add_argument("--annotate", action="store_true", help="Also print GitHub Actions annotations.")
        parser.add_argument("--strict", action="store_true", help="Exit 1 if any check FAILs.")
        parser.add_argument("--skip-feeds", action="store_true", help="Do not contact the news sources.")

    # -- output ----------------------------------------------------------------
    def _emit(self, status, section, detail):
        line = f"{status} {section}: {detail}"
        self.results.append((status, section, detail))
        self.stdout.write(line)

    def _write_annotations(self):
        """One GitHub annotation per GROUP, at the level of its worst finding. GitHub keeps only 10
        notices / 10 warnings / 10 errors per step, so one annotation per finding silently dropped
        most of the OK results; grouped, every result stays readable on the run's checks."""
        groups = {}
        for status, section, detail in self.results:
            groups.setdefault(ANNOTATION_GROUPS.get(section, "other"), []).append((status, section, detail))
        for group, findings in groups.items():
            worst = max((status for status, _s, _d in findings), key=lambda status: SEVERITY[status])
            level = {"OK": "notice", "SKIP": "notice", "WARN": "warning", "FAIL": "error"}[worst]
            body = " | ".join(f"{status} {section}: {detail}" for status, section, detail in findings)
            self.stdout.write(f"::{level} title=ops_verify {group}::{body}")

    def _run(self, name, func):
        """One section failing must never stop the others."""
        try:
            func()
        except Exception as exc:  # noqa: BLE001 - report, do not crash the check
            self._emit("FAIL", name, f"check itself errored: {type(exc).__name__}")

    def handle(self, *args, **options):
        self.annotate = options["annotate"]
        self.results = []
        sections = [
            ("release", self.check_release),
            ("migrations", self.check_migrations),
            ("schema", self.check_schema),
            ("database", self.check_database),
            ("redis", self.check_redis),
            ("celery", self.check_celery),
            ("beat", self.check_beat_schedule),
            ("providers", self.check_providers),
            ("ai", self.check_ai),
            ("capacity", self.check_capacity),
            ("frontend", self.check_frontend),
            ("disk", self.check_disk),
            ("logs", self.check_logs),
            ("retention", self.check_retention),
            ("backups", self.check_backups),
            ("settings", self.check_settings),
        ]
        if options["skip_feeds"]:
            self._emit("SKIP", "feeds", "skipped by --skip-feeds")
        else:
            sections.insert(8, ("feeds", self.check_feeds))
        for name, func in sections:
            self._run(name, func)
        failed = sum(1 for status, _s, _d in self.results if status == "FAIL")
        self.stdout.write(f"SUMMARY: {len(self.results)} findings, {failed} FAIL")
        if self.annotate:
            self._write_annotations()
        if options["strict"] and failed:
            raise SystemExit(1)

    # -- sections --------------------------------------------------------------
    def check_release(self):
        sha = getattr(settings, "RELEASE_SHA", "") or ""
        if sha and sha != "unknown":
            self._emit("OK", "release", f"this process was built from {sha[:12]}")
        else:
            self._emit("WARN", "release", "RELEASE_SHA is not set for this process (revision cannot be confirmed)")

    def check_migrations(self):
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        applied = {name for app, name in executor.loader.applied_migrations if app == "chat"}
        for name in EXPECTED_CHAT_MIGRATIONS:
            self._emit(
                "OK" if name in applied else "FAIL",
                "migrations",
                f"chat.{name}: {'applied' if name in applied else 'NOT applied'}",
            )
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        self._emit(
            "OK" if not pending else "FAIL", "migrations", f"unapplied migrations across all apps: {len(pending)}"
        )

    def check_schema(self):
        """Read-only: does every model field have its column? Uses introspection only."""
        from django.apps import apps

        with connection.cursor() as cursor:
            existing_tables = set(connection.introspection.table_names(cursor))
            missing, checked = [], 0
            for model in apps.get_models():
                table = model._meta.db_table
                if not model._meta.managed or model._meta.proxy:
                    continue
                if table not in existing_tables:
                    missing.append(f"table {table}")
                    continue
                columns = {c.name for c in connection.introspection.get_table_description(cursor, table)}
                for field in model._meta.concrete_fields:
                    checked += 1
                    if field.column not in columns:
                        missing.append(f"{table}.{field.column}")
            for table, wanted in EXPECTED_COLUMNS.items():
                columns = {c.name for c in connection.introspection.get_table_description(cursor, table)}
                for column in wanted:
                    self._emit(
                        "OK" if column in columns else "FAIL",
                        "schema",
                        f"{table}.{column}: {'present' if column in columns else 'MISSING'}",
                    )
        self._emit(
            "OK" if not missing else "FAIL",
            "schema",
            f"{checked} model columns checked, {len(missing)} missing" + (f": {missing[:5]}" if missing else ""),
        )

    def check_database(self):
        settings_dict = connection.settings_dict
        option_keys = sorted((settings_dict.get("OPTIONS") or {}).keys())  # names only, never values
        self._emit(
            "OK",
            "database",
            f"engine={connection.vendor} CONN_MAX_AGE={settings_dict.get('CONN_MAX_AGE')} option_keys={option_keys}",
        )
        if connection.vendor != "postgresql":
            self._emit("SKIP", "database", "server statistics are PostgreSQL-only")
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('max_connections')::int, "
                "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())"
            )
            limit, in_use = cursor.fetchone()
            self._emit("OK" if in_use < limit * 0.8 else "WARN", "database", f"connections {in_use}/{limit}")
            cursor.execute(
                "SELECT current_setting('statement_timeout'), current_setting('idle_in_transaction_session_timeout')"
            )
            statement, idle = cursor.fetchone()
            self._emit("OK", "database", f"statement_timeout={statement} idle_in_transaction_session_timeout={idle}")
            cursor.execute("SELECT pg_database_size(current_database())")
            self._emit("OK", "database", f"database size {_human(cursor.fetchone()[0])}")
            cursor.execute(
                "SELECT relname, pg_total_relation_size(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 2 DESC LIMIT 5"
            )
            self._emit(
                "OK",
                "database",
                "largest tables: " + ", ".join(f"{name}={_human(size)}" for name, size in cursor.fetchall()),
            )
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction' AND now() - state_change > interval '60 seconds'"
            )
            stuck = cursor.fetchone()[0]
            self._emit(
                "OK" if not stuck else "WARN", "database", f"connections idle in a transaction for over 60s: {stuck}"
            )

    def check_redis(self):
        from config.health import check_redis

        state = check_redis()["state"]
        if state == "not_configured":
            self._emit("SKIP", "redis", "REDIS_URL is not configured")
            return
        self._emit("OK" if state == "healthy" else "FAIL", "redis", f"PING via the shared health probe: {state}")
        key = f"ops_verify:{uuid.uuid4().hex}"
        cache.set(key, "1", timeout=15)  # expires by itself; nothing is deleted or flushed
        self._emit(
            "OK" if cache.get(key) == "1" else "FAIL", "redis", "cache write/read round trip on a 15s throw-away key"
        )
        self._check_redis_memory()

    def _check_redis_memory(self):
        """Redis memory as Redis reports it (INFO memory): used, peak, and whether a maxmemory bound exists.
        Read-only. Redis here holds the Celery queue, the cache and the rate-limit counters, so no eviction policy is
        assumed: an unbounded Redis is reported, not silently changed."""
        import redis

        client = None
        try:
            client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
            info = client.info("memory")
        except Exception as exc:  # noqa: BLE001 - a report line must never abort the run
            self._emit("WARN", "redis", f"memory not readable: {type(exc).__name__}")
            return
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
        limit = int(info.get("maxmemory", 0) or 0)
        summary = (
            f"memory: used {info.get('used_memory_human', '?')}, peak {info.get('used_memory_peak_human', '?')}, "
            f"maxmemory {_human(limit) if limit else 'not set (unbounded)'}, policy {info.get('maxmemory_policy', '?')}"
        )
        self._emit("OK" if limit else "WARN", "redis", summary)

    def check_celery(self):
        if not settings.REDIS_URL:
            self._emit("SKIP", "celery", "no broker configured (tasks run inline)")
            return
        from config.celery import app

        replies = app.control.inspect(timeout=3).ping() or {}
        self._emit("OK" if replies else "FAIL", "celery", f"workers answering a control ping: {len(replies)}")

    def check_beat_schedule(self):
        from django_celery_beat.models import PeriodicTask

        tasks = list(PeriodicTask.objects.values_list("task", "enabled"))
        enabled = [task for task, on in tasks if on]
        duplicated = sorted({task for task in enabled if enabled.count(task) > 1})
        self._emit("OK", "beat", f"{len(enabled)} enabled scheduled tasks ({len(tasks) - len(enabled)} disabled)")
        self._emit(
            "OK" if not duplicated else "WARN", "beat", f"tasks scheduled more than once: {duplicated or 'none'}"
        )
        # Beat has no health endpoint (its process is PID 1 of its container, so a crash
        # already restarts it). A scheduler that is alive but stuck shows up as stale runs.
        newest = (
            PeriodicTask.objects.filter(enabled=True, last_run_at__isnull=False)
            .order_by("-last_run_at")
            .values_list("last_run_at", flat=True)
            .first()
        )
        if newest is None:
            self._emit("WARN", "beat", "no enabled task has ever been dispatched (last_run_at is empty everywhere)")
        else:
            hours = (timezone.now() - newest).total_seconds() / 3600
            self._emit(
                "OK" if hours < 26 else "WARN",
                "beat",
                f"most recent dispatch was {hours:.1f}h ago (daily tasks expected within 26h)",
            )
        self._check_task_outcomes()

    def _check_task_outcomes(self):
        """Outcome of each enabled task as recorded by the worker's Celery signals
        (governance/task_monitor.py): failures, unrecorded outcomes, overdue dispatches."""
        from django_celery_beat.models import PeriodicTask

        from governance import task_monitor

        enabled = list(PeriodicTask.objects.select_related("interval").filter(enabled=True))
        failing, unrecorded, late, running = [], 0, [], 0
        for task in enabled:
            summary = task_monitor.summarize(task.task)
            if summary["outcome"] == "failing":
                failing.append(f"{task.name} ({summary['last_failure_kind'] or 'error'})")
            if not summary["recorded"]:
                unrecorded += 1
            running += summary["running"]
            if task_monitor.is_stale(task):
                late.append(task.name)
        self._emit("OK" if not failing else "FAIL", "beat", f"tasks whose last run failed: {failing or 'none'}")
        self._emit(
            "OK" if not late else "WARN",
            "beat",
            f"interval tasks dispatched later than 3 intervals ago: {late or 'none'}",
        )
        self._emit(
            "OK" if unrecorded < len(enabled) or not enabled else "WARN",
            "beat",
            f"enabled tasks with a recorded outcome: {len(enabled) - unrecorded}/{len(enabled)}; running now: {running}"
            " (records live in the cache, so none is expected right after a deploy or a cache flush)",
        )

    def check_providers(self):
        from providers.models import Provider

        for provider in Provider.objects.all().order_by("slug"):
            age = f"{(timezone.now() - provider.last_synced_at).days}d ago" if provider.last_synced_at else "never"
            status = "OK" if provider.is_connected and provider.last_sync_status == "success" else "WARN"
            self._emit(
                status,
                "providers",
                f"{provider.slug}: connected={provider.is_connected} last_sync={provider.last_sync_status} ({age})",
            )

    def check_ai(self):
        """Today's provider-call counters (cache) and token metadata actually stored on replies."""
        from chat import ai_metrics
        from chat.models import Message

        summary = ai_metrics.summary(days=1)
        total = summary["total"]
        if not total["requests"]:
            self._emit(
                "OK", "ai", "no provider calls counted today (counters live in the cache, so a flush resets them)"
            )
        else:
            failure_rate = total["failure"] / total["requests"]
            self._emit(
                "OK" if failure_rate < 0.2 else "WARN",
                "ai",
                f"today: {total['requests']} provider calls, {total['success']} ok, {total['failure']} failed "
                f"({total['timeout']} timeouts, {total['rate_limited']} rate-limited), "
                f"{total['fallback_success']} answered by a fallback model, {total['truncated']} cut short, "
                f"avg latency {total['avg_latency_ms']} ms",
            )
            for row in summary["rows"][:6]:
                self._emit(
                    "OK",
                    "ai",
                    f"{row['provider']}/{row['model']}: {row['requests']} calls, {row['failure']} failed, "
                    f"avg {row['avg_latency_ms']} ms",
                )
        since = timezone.now() - timedelta(hours=24)
        replies = Message.objects.filter(
            role=Message.Role.ASSISTANT,
            created_at__gte=since,
            provider_model_used__isnull=False,
            served_from_cache=False,
        )
        count = replies.count()
        with_tokens = replies.filter(input_tokens__isnull=False, output_tokens__isnull=False).count()
        sums = replies.aggregate(i=Sum("input_tokens"), o=Sum("output_tokens"))
        self._emit(
            "OK" if count == with_tokens else "WARN",
            "ai",
            f"replies in 24h carrying provider token counts (cache hits excluded): {with_tokens}/{count}; "
            f"input {sums['i'] or 0} output {sums['o'] or 0} tokens (as reported by the providers)",
        )

    @staticmethod
    def _memory_summary():
        """Memory as this container sees it (Linux only): what the host reports, and the container's own
        limit when a cgroup sets one. Read from /proc and /sys; nothing is executed."""
        parts = []
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                info = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in handle if ":" in line}
            parts.append(f"total {_human(info['MemTotal'])}, available {_human(info['MemAvailable'])}")
        except (OSError, KeyError, ValueError):
            return "memory: not readable here (not Linux)"
        for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                raw = open(path, encoding="ascii").read().strip()
            except OSError:
                continue
            if raw.isdigit() and int(raw) < 1 << 60:
                parts.append(f"container limit {_human(int(raw))}")
            break
        return "memory: " + ", ".join(parts)

    def check_frontend(self):
        """Browser-side failures reported by static/js/client-errors.js, counted today (cache)."""
        from config import client_errors

        counts = client_errors.summary(days=1)
        if not counts:
            self._emit("OK", "frontend", "no browser-side errors reported today")
            return
        parts = ", ".join(f"{kind}/{status or 'n-a'}={n}" for (kind, status), n in sorted(counts.items()))
        self._emit("WARN", "frontend", f"browser-side errors reported today: {parts}")

    def check_capacity(self):
        """Evidence for the Gunicorn sizing question: how many chat replies are streaming right now
        compared with the request threads that exist. Read-only."""
        from chat.models import Message

        workers = int(os.environ.get("GUNICORN_WORKERS", 3))
        threads = int(os.environ.get("GUNICORN_THREADS", 4))
        capacity = workers * threads
        live = Message.objects.filter(
            is_generating=True, generation_started_at__gte=timezone.now() - timedelta(minutes=10)
        )
        stuck = Message.objects.filter(
            is_generating=True, generation_started_at__lt=timezone.now() - timedelta(minutes=10)
        )
        streaming = live.count()
        self._emit(
            "OK" if streaming < capacity * 0.75 else "WARN",
            "capacity",
            f"chat replies streaming right now: {streaming} of {capacity} request threads "
            f"({workers} workers x {threads}); each open reply holds one thread for its whole duration",
        )
        self._emit(
            "OK" if not stuck.count() else "WARN",
            "capacity",
            f"replies claimed as generating for over 10 min: {stuck.count()}",
        )
        self._emit("OK", "capacity", f"CPU cores visible to this container: {os.cpu_count()}")
        self._emit("OK", "capacity", self._memory_summary())

    def check_feeds(self):
        from chat import live_intelligence as li

        if not li.enabled():
            self._emit("SKIP", "feeds", "Live Intelligence is switched off")
            return
        seen = set()
        for category in li.CATEGORIES.values():
            for source in category["sources"]:
                name, url, _kind = source
                if url in seen:
                    continue
                seen.add(url)
                stories, ok = li._fetch_source(source)  # exactly one GET per source
                host = url.split("/")[2]
                verdict = f"REACHABLE, {len(stories)} usable stories" if ok else "UNREACHABLE (1 attempt)"
                self._emit(
                    "OK" if ok and stories else ("WARN" if ok else "FAIL"),
                    "feeds",
                    f"{name} ({host}): {verdict}",
                )

    def check_disk(self):
        for label, path in (("app volume", str(settings.BASE_DIR)), ("root", "/")):
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            pct = usage.used / usage.total * 100
            self._emit(
                "OK" if pct < 80 else ("WARN" if pct < 90 else "FAIL"),
                "disk",
                f"{label}: {pct:.0f}% used ({_human(usage.free)} free of {_human(usage.total)})",
            )
        for label, path in (
            ("media", settings.MEDIA_ROOT),
            ("logs", settings.BASE_DIR / "logs"),
            ("staticfiles", getattr(settings, "STATIC_ROOT", None)),
        ):
            if path and os.path.isdir(path):
                count, size = _dir_stats(path)
                self._emit("OK", "disk", f"{label}: {count} files, {_human(size)}")
        self._check_media_orphans()

    def _check_media_orphans(self):
        """Files on disk that no record refers to. Counts only; nothing is deleted automatically
        (review and delete one at a time in Server Media)."""
        from governance import media_service

        scan = media_service.run_scan()
        if scan["error"]:
            self._emit("WARN", "disk", f"media scan failed: {scan['error']}")
            return
        partial = " (partial scan)" if scan["partial"] else ""
        self._emit(
            "OK" if not scan["orphan_count"] else "WARN",
            "disk",
            f"media orphan candidates: {scan['orphan_count']} files, {_human(scan['orphan_bytes'])}; "
            f"records pointing at a missing file: {scan['missing_count']}{partial}",
        )

    def check_logs(self):
        """Counts lines that still contain a credential-looking ?key= value (from before
        the Gemini key moved into a header). A number only - never the content."""
        log_dir = settings.BASE_DIR / "logs"
        total = files = 0
        if os.path.isdir(log_dir):
            for name in os.listdir(log_dir):
                if name.startswith("app.log"):
                    files += 1
                    with open(os.path.join(log_dir, name), encoding="utf-8", errors="ignore") as handle:
                        total += sum(1 for line in handle if _LEAKED_KEY.search(line))
        if not files:
            self._emit("SKIP", "logs", "no app.log in this container")
        else:
            self._emit(
                "OK" if total == 0 else "WARN",
                "logs",
                f"{total} log lines across {files} file(s) contain an un-redacted ?key=/token= value",
            )

    def check_retention(self):
        from django.apps import apps

        for label, model_path in (
            ("audit log", "governance.AuditLog"),
            ("email log", "notifications.EmailLog"),
            ("notifications", "notifications.Notification"),
            ("conversations", "chat.Conversation"),
            ("messages", "chat.Message"),
        ):
            model = apps.get_model(model_path)
            field = "created_at" if any(f.name == "created_at" for f in model._meta.fields) else None
            oldest = model.objects.order_by(field).values_list(field, flat=True).first() if field else None
            self._emit(
                "OK", "retention", f"{label}: {model.objects.count()} rows, oldest {oldest.date() if oldest else 'n/a'}"
            )
        self._emit("OK", "retention", f"BACKUP_RETENTION_DAYS={getattr(settings, 'BACKUP_RETENTION_DAYS', 'unset')}")

    def check_backups(self):
        """Whether the nightly database backup has anywhere to go and, when it does, what the destination
        actually holds: the newest backup's age and size (one read-only listing; nothing is downloaded,
        written or deleted). Credentials are never printed - only counts, sizes and ages."""
        from accounts.management.commands import backup_database as backup

        if not backup.is_configured():
            self._emit("WARN", "backups", "no S3 backup target configured: backup_database has nowhere to upload")
            return
        self._emit("OK", "backups", f"S3 backup target configured (retention {settings.BACKUP_RETENTION_DAYS} days)")
        try:
            found = backup.list_backups()
        except Exception as exc:  # noqa: BLE001
            self._emit("FAIL", "backups", f"the backup destination could not be read ({type(exc).__name__})")
            return
        if not found:
            self._emit("FAIL", "backups", "the destination is reachable but holds no backups yet")
            return
        _key, size, modified = found[0]
        hours = (timezone.now() - modified).total_seconds() / 3600
        self._emit(
            "OK" if hours < 26 else "WARN",
            "backups",
            f"newest backup {hours:.1f}h old, {_human(size)}; {len(found)} kept (daily backup expected within 26h)",
        )

    def check_settings(self):
        self._emit("OK" if not settings.DEBUG else "FAIL", "settings", f"DEBUG={settings.DEBUG}")
        # Presence only (never a value) of the variables production depends on.
        present = {
            name: bool(os.environ.get(name) or getattr(settings, name, ""))
            for name in ("REDIS_URL", "SITE_URL", "FIELD_ENCRYPTION_KEY", "SENTRY_DSN")
        }
        missing = sorted(name for name, ok in present.items() if not ok)
        self._emit("OK" if not missing else "WARN", "settings", f"required settings not set: {missing or 'none'}")
        # docker-compose.yml falls back to a public placeholder database password when the server's .env sets none.
        # Boolean only: the password itself is never read into the output.
        db = settings.DATABASES["default"]
        if db.get("ENGINE", "").endswith("postgresql"):
            is_default = db.get("PASSWORD") == COMPOSE_DEFAULT_DB_PASSWORD
            self._emit(
                "FAIL" if is_default else "OK",
                "settings",
                "database password is the public compose placeholder: "
                + ("YES - set POSTGRES_PASSWORD" if is_default else "no"),
            )
        # Who hears about a crash: ADMINS if set, else the active SuperAdmins (governance/error_alerts.py). Counts only.
        from governance.error_alerts import alert_recipients

        recipients = len(alert_recipients())
        source = "ADMINS" if settings.ADMINS else "active SuperAdmins (ADMINS is not set)"
        self._emit(
            "OK" if recipients else "WARN", "settings", f"crash alerts go to {recipients} recipient(s): {source}"
        )
        # Transport security as configured for THIS process (booleans and a number, nothing else).
        self._emit(
            (
                "OK"
                if settings.SESSION_COOKIE_SECURE
                and settings.CSRF_COOKIE_SECURE
                and settings.ENFORCE_HTTPS_VIA_CLOUDFLARE
                else "WARN"
            ),
            "settings",
            f"session_cookie_secure={settings.SESSION_COOKIE_SECURE} csrf_cookie_secure={settings.CSRF_COOKIE_SECURE} "
            f"https_redirect={settings.ENFORCE_HTTPS_VIA_CLOUDFLARE} hsts_seconds={settings.CLOUDFLARE_HSTS_SECONDS}",
        )
        sentry = bool(getattr(settings, "SENTRY_DSN", ""))
        backend = settings.EMAIL_BACKEND.rsplit(".", 1)[-1]
        self._emit(
            "OK", "settings", f"Sentry configured={sentry} ADMINS={len(settings.ADMINS)} email_backend={backend}"
        )
        import io

        buffer = io.StringIO()
        try:
            call_command("check", deploy=True, stdout=buffer, stderr=buffer)
            ids = sorted(set(re.findall(r"\((security\.W\d+)\)", buffer.getvalue())))
            self._emit("OK" if not ids else "WARN", "settings", f"check --deploy warnings: {ids or 'none'}")
        except Exception as exc:  # noqa: BLE001 - check --deploy raises on errors
            self._emit("FAIL", "settings", f"check --deploy raised {type(exc).__name__}")
