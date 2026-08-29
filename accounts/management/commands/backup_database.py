import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "db-backups/"


class Command(BaseCommand):
    help = (
        "Dump the production PostgreSQL database with pg_dump, upload it to "
        "S3-compatible object storage, and delete backups older than "
        "BACKUP_RETENTION_DAYS. Intended to run on a daily schedule (see "
        "docs/BACKUP_RESTORE.md for exact setup and restore commands). "
        "No-ops with a clear message on SQLite, since pg_dump doesn't apply."
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

        if not settings.BACKUP_S3_BUCKET:
            raise CommandError(
                "BACKUP_S3_BUCKET is not set. Refusing to run a backup with nowhere to store it — "
                "see docs/BACKUP_RESTORE.md for the required environment variables."
            )

        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"backup-{timestamp}.dump"

        with tempfile.TemporaryDirectory() as tmpdir:
            dump_path = Path(tmpdir) / filename
            self._dump(db, dump_path)
            size_mb = dump_path.stat().st_size / (1024 * 1024)
            self.stdout.write(f"Dump created: {dump_path.name} ({size_mb:.1f} MB)")
            self._upload(dump_path, filename)

        pruned = self._prune_old_backups()

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
            raise CommandError(f"pg_dump failed (exit {result.returncode}): {result.stderr}")

    def _s3_client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=settings.BACKUP_S3_ENDPOINT_URL or None,
            aws_access_key_id=settings.BACKUP_S3_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.BACKUP_S3_SECRET_ACCESS_KEY or None,
            region_name=settings.BACKUP_S3_REGION or None,
        )

    def _upload(self, dump_path, filename):
        key = f"{BACKUP_PREFIX}{filename}"
        self._s3_client().upload_file(str(dump_path), settings.BACKUP_S3_BUCKET, key)
        self.stdout.write(f"Uploaded to s3://{settings.BACKUP_S3_BUCKET}/{key}")

    def _prune_old_backups(self):
        cutoff = datetime.now(dt_timezone.utc) - timedelta(days=settings.BACKUP_RETENTION_DAYS)
        client = self._s3_client()
        paginator = client.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=settings.BACKUP_S3_BUCKET, Prefix=BACKUP_PREFIX):
            for obj in page.get("Contents", []):
                if obj["LastModified"] < cutoff:
                    client.delete_object(Bucket=settings.BACKUP_S3_BUCKET, Key=obj["Key"])
                    deleted += 1
        if deleted:
            self.stdout.write(f"Pruned {deleted} backup(s) older than {settings.BACKUP_RETENTION_DAYS} days.")
        return deleted
