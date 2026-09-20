"""The database backup must report what really happened.

Regression for: a backup that FAILED was indistinguishable from one that was never configured (both were
"a warning in a server file"), the upload was never confirmed, and retention could delete every backup once
the schedule stopped. None of this needs a real bucket or Postgres: the destination and pg_dump are doubled,
so these tests prove the command's decisions, not S3 itself."""

from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from accounts.management.commands import backup_database as backup
from accounts.management.commands.backup_database import BackupFailed, BackupNotConfigured

PG = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "db",
        "USER": "u",
        "HOST": "h",
        "PORT": "5432",
        "PASSWORD": "pw",
    }
}
CONFIGURED = {"BACKUP_S3_BUCKET": "bucket", "BACKUP_S3_ACCESS_KEY_ID": "id", "BACKUP_S3_SECRET_ACCESS_KEY": "secret"}
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=dt_timezone.utc)
REAL_DUMP = backup.Command._dump  # before any test replaces it


def objects(*ages_days, name="backup-20260101-030000.dump"):
    return [
        (f"db-backups/backup-2026{n:04d}-030000.dump", 1000 + n, NOW - timedelta(days=age))
        for n, age in enumerate(ages_days, start=1)
    ]


def run(*args):
    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


class FakeS3:
    def __init__(self, stored_size=None, upload_error=None):
        self.stored_size, self.upload_error = stored_size, upload_error
        self.uploaded, self.deleted = None, []

    def upload_file(self, path, bucket, key):
        if self.upload_error:
            raise self.upload_error
        self.uploaded = (Path(path).stat().st_size, key)

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3's own argument names
        return {"ContentLength": self.stored_size if self.stored_size is not None else self.uploaded[0]}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)

    def get_paginator(self, _name):
        return mock.Mock(paginate=lambda **kw: [{"Contents": []}])


def fake_dump(self, db, path):
    Path(path).write_bytes(b"PGDMP" + b"x" * 200)


@override_settings(**CONFIGURED, BACKUP_RETENTION_DAYS=30)
class BackupCommandTests(SimpleTestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(settings, "DATABASES", PG),
            mock.patch.object(backup.Command, "_dump", fake_dump),
            mock.patch.object(backup.Command, "_prune_old_backups", lambda self: 0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def command_with(self, fake):
        return mock.patch.object(backup.Command, "_s3_client", lambda self: fake)

    def test_a_verified_upload_is_a_success(self):
        fake = FakeS3()
        with self.command_with(fake):
            output = run("backup_database")
        self.assertIn("Uploaded and verified", output)
        self.assertIn("Backup complete", output)
        self.assertTrue(fake.uploaded[1].startswith("db-backups/backup-"))

    @override_settings(BACKUP_S3_BUCKET="")
    def test_no_bucket_is_exit_status_3_not_a_failure(self):
        with self.assertRaises(BackupNotConfigured) as raised:
            run("backup_database")
        self.assertEqual(raised.exception.returncode, backup.EXIT_NOT_CONFIGURED)

    def test_a_pg_dump_failure_is_exit_status_4(self):
        failed = mock.Mock(returncode=1, stderr="pg_dump: error: connection refused")
        untouched = FakeS3()  # a failed dump must never reach the destination
        with mock.patch.object(backup.Command, "_dump", REAL_DUMP), mock.patch.object(
            backup.subprocess, "run", return_value=failed
        ), self.command_with(untouched):
            with self.assertRaises(BackupFailed) as raised:
                run("backup_database")
        self.assertEqual(raised.exception.returncode, backup.EXIT_FAILED)
        self.assertIn("pg_dump failed", str(raised.exception))
        self.assertIsNone(untouched.uploaded)

    def test_an_upload_error_is_a_failure_and_the_message_carries_no_detail(self):
        fake = FakeS3(upload_error=ConnectionError("https://secret-bucket.example/key=abc failed"))
        with self.command_with(fake), self.assertRaises(BackupFailed) as raised:
            run("backup_database")
        self.assertEqual(raised.exception.returncode, backup.EXIT_FAILED)
        self.assertIn("ConnectionError", str(raised.exception))
        self.assertNotIn("secret-bucket", str(raised.exception))

    def test_an_upload_that_arrived_with_the_wrong_size_is_a_failure(self):
        with self.command_with(FakeS3(stored_size=3)), self.assertRaises(BackupFailed) as raised:
            run("backup_database")
        self.assertIn("bytes", str(raised.exception))

    def test_a_retention_error_does_not_fail_a_backup_that_was_stored(self):
        def boom(self):
            raise RuntimeError("list denied")

        with self.command_with(FakeS3()), mock.patch.object(backup.Command, "_prune_old_backups", boom):
            with self.assertLogs("accounts.management.commands.backup_database", level="ERROR"):
                output = run("backup_database")
        self.assertIn("Backup complete", output)

    def test_sqlite_is_a_harmless_no_op(self):
        with mock.patch.object(
            settings, "DATABASES", {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
        ):
            self.assertIn("nothing to back up", run("backup_database"))


class RetentionTests(SimpleTestCase):
    def test_only_backups_older_than_the_window_are_removed(self):
        found = objects(1, 2, 3, 40, 50)  # newest first
        self.assertEqual(backup.plan_pruning(found, 30, now=NOW), [found[3][0], found[4][0]])

    def test_the_newest_backups_survive_even_when_every_one_is_old(self):
        found = objects(100, 110, 120, 130, 140)
        doomed = backup.plan_pruning(found, 30, now=NOW)
        self.assertEqual(doomed, [found[3][0], found[4][0]])
        self.assertEqual(len(found) - len(doomed), backup.MIN_BACKUPS_KEPT)

    def test_a_short_history_is_never_pruned(self):
        self.assertEqual(backup.plan_pruning(objects(90, 100), 30, now=NOW), [])

    def test_nothing_that_is_not_a_backup_file_is_ever_listed_for_deletion(self):
        client = mock.Mock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "db-backups/backup-20260101-030000.dump", "Size": 5, "LastModified": NOW},
                    {"Key": "db-backups/notes.txt", "Size": 5, "LastModified": NOW - timedelta(days=999)},
                    {"Key": "db-backups/backup-latest.dump", "Size": 5, "LastModified": NOW - timedelta(days=999)},
                ]
            }
        ]
        with override_settings(BACKUP_S3_BUCKET="b"):
            self.assertEqual(
                [k for k, _s, _m in backup.list_backups(client)], ["db-backups/backup-20260101-030000.dump"]
            )

    @override_settings(**CONFIGURED, BACKUP_RETENTION_DAYS=30)
    def test_the_command_deletes_exactly_what_the_plan_says(self):
        found = objects(1, 2, 3, 40)
        fake = FakeS3()
        with mock.patch.object(backup, "list_backups", return_value=found), mock.patch.object(
            backup.Command, "_s3_client", lambda self: fake
        ):
            backup.Command()._prune_old_backups()
        self.assertEqual(fake.deleted, [found[3][0]])


class ScheduledTaskTests(SimpleTestCase):
    def test_not_configured_is_a_warning_and_never_raises(self):
        from accounts.tasks import run_scheduled_database_backup

        with mock.patch("accounts.tasks.call_command", side_effect=BackupNotConfigured("no bucket")):
            with self.assertLogs("accounts.tasks", level="WARNING") as logs:
                run_scheduled_database_backup()
        self.assertTrue(all(r.levelname == "WARNING" for r in logs.records))

    def test_a_configured_backup_that_failed_raises_so_it_is_retried_and_visible(self):
        from accounts.tasks import run_scheduled_database_backup

        with mock.patch("accounts.tasks.call_command", side_effect=BackupFailed("pg_dump failed")):
            with self.assertLogs("accounts.tasks", level="ERROR"), self.assertRaises(CommandError):
                run_scheduled_database_backup()


@override_settings(**CONFIGURED)
class OpsVerifyAndVerifyBackupTests(SimpleTestCase):
    def ops(self):
        from governance.management.commands import ops_verify

        command = ops_verify.Command()
        command.results = []
        command.stdout = StringIO()
        command.check_backups()
        return [f"{status} {detail}" for status, _section, detail in command.results]

    def test_ops_verify_reports_the_newest_backup_without_credentials(self):
        recent = [("db-backups/backup-20260921-030000.dump", 2048, datetime.now(dt_timezone.utc) - timedelta(hours=3))]
        with mock.patch.object(backup, "list_backups", return_value=recent):
            lines = self.ops()
        self.assertTrue(any(line.startswith("OK newest backup 3.0h old") for line in lines), lines)
        self.assertNotIn("secret", " ".join(lines))

    def test_ops_verify_warns_about_a_stale_backup_and_fails_on_an_empty_or_unreadable_bucket(self):
        old = [("db-backups/backup-20260101-030000.dump", 10, datetime.now(dt_timezone.utc) - timedelta(days=3))]
        with mock.patch.object(backup, "list_backups", return_value=old):
            self.assertTrue(any(line.startswith("WARN newest backup") for line in self.ops()))
        with mock.patch.object(backup, "list_backups", return_value=[]):
            self.assertTrue(any(line.startswith("FAIL") and "no backups" in line for line in self.ops()))
        with mock.patch.object(backup, "list_backups", side_effect=ConnectionError("nope")):
            self.assertTrue(any(line.startswith("FAIL") and "ConnectionError" in line for line in self.ops()))

    @override_settings(BACKUP_S3_BUCKET="")
    def test_ops_verify_says_so_when_nothing_is_configured(self):
        self.assertTrue(any(line.startswith("WARN no S3 backup target") for line in self.ops()))

    def test_verify_backup_needs_a_destination_and_a_backup(self):
        with override_settings(BACKUP_S3_BUCKET=""), self.assertRaises(BackupNotConfigured):
            run("verify_backup")
        with mock.patch.object(backup, "list_backups", return_value=[]), self.assertRaises(CommandError):
            run("verify_backup")

    def test_verify_backup_reads_the_newest_backup_and_restores_nothing(self):
        found = [("db-backups/backup-20260921-030000.dump", 5, datetime.now(dt_timezone.utc) - timedelta(hours=1))]
        client = mock.Mock()
        client.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(b"12345")
        listing = "; header\n1; 1259 16386 TABLE public accounts_user u\n2; 0 16400 TABLE DATA public accounts_user u\n"
        done = mock.Mock(returncode=0, stdout=listing, stderr="")
        with mock.patch.object(backup, "list_backups", return_value=found), mock.patch.object(
            backup, "s3_client", return_value=client
        ), mock.patch("accounts.management.commands.verify_backup.subprocess.run", return_value=done) as pg_restore:
            output = run("verify_backup")
        self.assertIn("Readable: 2 objects, 1 tables with data.", output)
        self.assertEqual(
            pg_restore.call_args.args[0][:2], ["pg_restore", "--list"]
        )  # a table of contents, never a restore

    def test_verify_backup_fails_when_the_download_is_the_wrong_size_or_unreadable(self):
        found = [("db-backups/backup-20260921-030000.dump", 999, datetime.now(dt_timezone.utc))]
        client = mock.Mock()
        client.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(b"12345")
        with mock.patch.object(backup, "list_backups", return_value=found), mock.patch.object(
            backup, "s3_client", return_value=client
        ), self.assertRaises(CommandError):
            run("verify_backup")
        found[0] = (found[0][0], 5, found[0][2])
        broken = mock.Mock(returncode=1, stdout="", stderr="not a dump")
        with mock.patch.object(backup, "list_backups", return_value=found), mock.patch.object(
            backup, "s3_client", return_value=client
        ), mock.patch("accounts.management.commands.verify_backup.subprocess.run", return_value=broken):
            with self.assertRaises(CommandError):
                run("verify_backup")
