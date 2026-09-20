import os
import subprocess
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from accounts.management.commands import backup_database as backup


class Command(BaseCommand):
    help = (
        "Read-only proof that the NEWEST off-server backup is usable: find it, download it to a temp "
        "directory, and have pg_restore read its table of contents (`pg_restore --list`). It restores "
        "nothing and never touches the live database; the temp copy is deleted when the command ends. "
        "Exit status 0 = a readable backup exists, 1 = it does not, 3 = no bucket configured."
    )

    def handle(self, *args, **options):
        if not backup.is_configured():
            raise backup.BackupNotConfigured("The backup destination is not configured; nothing to verify.")
        try:
            backups = backup.list_backups()
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"The backup destination could not be read ({type(exc).__name__}).") from exc
        if not backups:
            raise CommandError("The bucket holds no backups.")
        key, size, modified = backups[0]
        age_hours = (datetime.now(dt_timezone.utc) - modified).total_seconds() / 3600
        self.stdout.write(f"Newest backup: {size / (1024 * 1024):.2f} MB, {age_hours:.1f} h old ({len(backups)} kept)")
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "verify.dump"
            try:
                from django.conf import settings

                backup.s3_client().download_file(settings.BACKUP_S3_BUCKET, key, str(local))
            except Exception as exc:  # noqa: BLE001
                raise CommandError(f"The newest backup could not be downloaded ({type(exc).__name__}).") from exc
            if local.stat().st_size != size:
                raise CommandError("The downloaded backup is not the size the bucket reports.")
            result = subprocess.run(
                ["pg_restore", "--list", str(local)], capture_output=True, text=True, env={**os.environ}
            )
        if result.returncode != 0:
            raise CommandError(f"pg_restore could not read the backup (exit {result.returncode}).")
        entries = [line for line in result.stdout.splitlines() if line and not line.startswith(";")]
        tables = sum(1 for line in entries if " TABLE DATA " in line)
        self.stdout.write(self.style.SUCCESS(f"Readable: {len(entries)} objects, {tables} tables with data."))
