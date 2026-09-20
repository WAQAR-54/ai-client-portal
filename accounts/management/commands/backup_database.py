import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "db-backups/"
BACKUP_NAME = re.compile(r"^backup-\d{8}-\d{6}\.dump$")
# Retention never removes the newest backups, whatever their age: if the schedule silently stopped, the
# age cut-off alone would eventually delete EVERY backup and leave nothing to restore from.
MIN_BACKUPS_KEPT = 3

# Exit codes (Django turns CommandError(returncode=...) into the process exit status), so the deploy
# script can tell "no backup was ever set up" from "a configured backup failed".
EXIT_NOT_CONFIGURED = 3
EXIT_FAILED = 4


class BackupNotConfigured(CommandError):
    """No bucket is configured: an expected pre-setup state, not a failure of a backup that exists."""

    def __init__(self, message):
        super().__init__(message, returncode=EXIT_NOT_CONFIGURED)


class BackupFailed(CommandError):
    """A configured backup did not complete (dump, upload or verification failed)."""

    def __init__(self, message):
        super().__init__(message, returncode=EXIT_FAILED)


def s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.BACKUP_S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.BACKUP_S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.BACKUP_S3_SECRET_ACCESS_KEY or None,
        region_name=settings.BACKUP_S3_REGION or None,
    )


def is_configured():
    """A destination exists when a bucket is named. Keys are optional here on purpose: boto3 can also
    take credentials from the environment or an instance role."""
    return bool(getattr(settings, "BACKUP_S3_BUCKET", ""))


def list_backups(client=None):
    """[(key, size, last_modified)] of the backups in the bucket, newest first. Read-only."""
    client = client or s3_client()
    found = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=settings.BACKUP_S3_BUCKET, Prefix=BACKUP_PREFIX
    ):
        for obj in page.get("Contents", []):
            if BACKUP_NAME.match(obj["Key"][len(BACKUP_PREFIX) :]):
                found.append((obj["Key"], obj["Size"], obj["LastModified"]))
    return sorted(found, key=lambda item: item[2], reverse=True)


def plan_pruning(backups, retention_days, now=None):
    """Keys that may be deleted: older than the retention window AND not among the newest MIN_BACKUPS_KEPT.
    `backups` is newest first, as list_backups returns it."""
    cutoff = (now or datetime.now(dt_timezone.utc)) - timedelta(days=retention_days)
    return [key for key, _size, modified in backups[MIN_BACKUPS_KEPT:] if modified < cutoff]


class Command(BaseCommand):
    help = (
        "Dump the production PostgreSQL database with pg_dump, upload it to "
        "S3-compatible object storage, confirm it arrived, and delete backups older than "
        "BACKUP_RETENTION_DAYS (never the newest few). Intended to run on a daily schedule (see "
        "docs/BACKUP_RESTORE.md for exact setup and restore commands). "
        "No-ops with a clear message on SQLite, since pg_dump doesn't apply. "
        "Exit status: 0 done, 3 no bucket configured, 4 a configured backup failed."
    )

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]

        if "postgresql" not in db["ENGINE"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Database engine is {db['ENGINE']!r}, not PostgreSQL — nothing to back up here. "
                    "This command only makes sense against the production database."
                )
            )
            return

        if not is_configured():
            raise BackupNotConfigured(
                "BACKUP_S3_BUCKET is not set. Refusing to run a backup with nowhere to store it — "
                "see docs/BACKUP_RESTORE.md for the required environment variables."
            )

        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"backup-{timestamp}.dump"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                dump_path = Path(tmpdir) / filename
                self._dump(db, dump_path)
                size = dump_path.stat().st_size
                self.stdout.write(f"Dump created: {dump_path.name} ({size / (1024 * 1024):.1f} MB)")
                self._upload(dump_path, filename, size)
        except BackupFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - boto/OS errors: reported by class, detail stays in the log
            logger.exception("Database backup failed")
            raise BackupFailed(f"The backup could not be completed ({type(exc).__name__}).") from exc

        try:
            pruned = self._prune_old_backups()
        except Exception:  # noqa: BLE001 - the new backup exists; a retention hiccup must not fail it
            logger.exception("Backup retention pruning failed (the new backup was stored)")
            pruned = 0

        logger.info("Database backup completed: %s (pruned %d old backup(s))", filename, pruned)
        self.stdout.write(self.style.SUCCESS(f"Backup complete: {filename}"))

    def _dump(self, db, dump_path):
        """pg_dump -Fc (custom format): compressed on its own, and restorable
        selectively/in-parallel with pg_restore — the standard choice over a
        plain .sql text dump for anything beyond a toy database."""
        cmd = [
            "pg_dump",
            "--host",
            db["HOST"] or "localhost",
            "--port",
            str(db["PORT"] or 5432),
            "--username",
            db["USER"],
            "--format",
            "custom",
            "--file",
            str(dump_path),
            "--no-password",  # never prompt interactively - PGPASSWORD env only
            db["NAME"],
        ]
        env = {**os.environ, "PGPASSWORD": db.get("PASSWORD") or ""}
        self.stdout.write("Running pg_dump...")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("pg_dump failed (exit %s): %s", result.returncode, (result.stderr or "")[:500])
            raise BackupFailed(f"pg_dump failed (exit {result.returncode}). See the server log for its message.")

    def _s3_client(self):
        return s3_client()

    def _upload(self, dump_path, filename, size):
        """Upload, then ask the destination what it now holds: a dump that 'uploaded' without error but is
        missing or a different size is a failed backup, not a successful one."""
        key = f"{BACKUP_PREFIX}{filename}"
        client = self._s3_client()
        client.upload_file(str(dump_path), settings.BACKUP_S3_BUCKET, key)
        stored = client.head_object(Bucket=settings.BACKUP_S3_BUCKET, Key=key)["ContentLength"]
        if stored != size:
            raise BackupFailed(f"The uploaded backup is {stored} bytes but the dump is {size} bytes.")
        self.stdout.write(f"Uploaded and verified: s3://{settings.BACKUP_S3_BUCKET}/{key} ({stored} bytes)")

    def _prune_old_backups(self):
        client = self._s3_client()
        doomed = plan_pruning(list_backups(client), settings.BACKUP_RETENTION_DAYS)
        for key in doomed:
            client.delete_object(Bucket=settings.BACKUP_S3_BUCKET, Key=key)
        if doomed:
            self.stdout.write(
                f"Pruned {len(doomed)} backup(s) older than {settings.BACKUP_RETENTION_DAYS} days "
                f"(the newest {MIN_BACKUPS_KEPT} are always kept)."
            )
        return len(doomed)
