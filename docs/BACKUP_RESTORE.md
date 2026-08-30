# Database backup & restore

## What this is

Automated daily backups of the production PostgreSQL database, uploaded to
S3-compatible object storage (a bucket on a different provider/account than
the app's own host — a backup that lives on the same server it protects
isn't a real backup). Retention: **30 days by default**, configurable via
`BACKUP_RETENTION_DAYS` — lower it if storage cost matters more than
history depth; there's no code change needed either way.

Implementation: `accounts/management/commands/backup_database.py`
(`python manage.py backup_database`). It no-ops with a clear message if run
against SQLite (local dev) — it's meant for the real Postgres database only.

## Required environment variables

Set these in the production environment (Railway service variables), not
in `.env` (which is dev-only and never committed):

| Variable | Meaning |
|---|---|
| `BACKUP_S3_BUCKET` | Bucket name to upload backups into |
| `BACKUP_S3_ENDPOINT_URL` | S3-compatible endpoint (e.g. Cloudflare R2, Backblaze B2). Leave unset for real AWS S3. |
| `BACKUP_S3_ACCESS_KEY_ID` | Access key for that bucket |
| `BACKUP_S3_SECRET_ACCESS_KEY` | Secret key for that bucket |
| `BACKUP_S3_REGION` | Region, if your provider needs one |
| `BACKUP_RETENTION_DAYS` | Optional, defaults to `30` |

The bucket/account running these backups should be **separate from
Railway** (a different provider or at minimum a different account) —
that's the whole point of an off-server backup.

## Scheduling it (Railway)

Railway does not run this automatically. Set it up as a **Railway Cron
Job** (a separate service in the same project):

1. In the Railway project, add a new service → "Cron Job" (or a normal
   service with a cron schedule set in its settings).
2. Point it at the same repo/image as the main app.
3. Schedule: `0 3 * * *` (daily at 03:00 UTC — pick an off-peak hour).
4. Start command: `python manage.py backup_database`
5. Give that service the same `DATABASE_URL` as the main app, plus the
   `BACKUP_S3_*` variables above.

Until that service exists, backups do not run — this file existing does
not mean backups are active. Confirm the cron service is actually
scheduled after deploying.

## Restore procedure (exact commands)

**This is the part that matters in an actual emergency — read it now, not
when you're already down.**

1. Find the backup you want. List what's in the bucket:
   ```bash
   aws s3 ls s3://$BACKUP_S3_BUCKET/db-backups/ --endpoint-url $BACKUP_S3_ENDPOINT_URL
   ```
   (Omit `--endpoint-url` for real AWS S3.)

2. Download it:
   ```bash
   aws s3 cp s3://$BACKUP_S3_BUCKET/db-backups/backup-20260101-030000.dump ./restore.dump \
     --endpoint-url $BACKUP_S3_ENDPOINT_URL
   ```

3. **Restore to a throwaway/staging database first, never directly onto
   production**, to confirm the dump is valid before touching anything real:
   ```bash
   createdb -h <staging-host> -U <staging-user> staging_restore_test
   pg_restore --host <staging-host> --username <staging-user> \
     --dbname staging_restore_test --no-owner --clean --if-exists \
     ./restore.dump
   ```
   Then sanity-check it (row counts, spot-check a few tables) before going further:
   ```bash
   psql -h <staging-host> -U <staging-user> -d staging_restore_test -c "SELECT count(*) FROM accounts_user;"
   ```

4. Only once that looks right, restore onto the real target (production,
   during a maintenance window — this drops and recreates objects that
   already exist, via `--clean --if-exists`):
   ```bash
   pg_restore --host <prod-host> --username <prod-user> \
     --dbname <prod-db-name> --no-owner --clean --if-exists \
     ./restore.dump
   ```

5. Restart the app service afterward so any in-memory/connection-pooled
   state doesn't reference pre-restore data.

## Status

**Retention: confirmed 30 days.** `BACKUP_RETENTION_DAYS` is unset in
`.env`, so the `settings.py` default of `30` is what's actually active —
matches the 14-30 day range agreed with the client; no code or config
change needed.

**The actual `pg_dump`/`pg_restore`/S3 path (steps above) is still
untested.** The environment this project is built in has no PostgreSQL
server, no `pg_dump`/`pg_restore` client tools, and no S3-compatible
bucket credentials — `backup_database.py` itself checks for a PostgreSQL
engine and refuses to run against anything else, so it cannot be exercised
here at all, not even to see it fail cleanly. This has not changed since
this doc was first written.

**What *was* tested for real, 2026-08-30, as a partial substitute**: a
logical backup/restore cycle against this environment's actual SQLite dev
database, using Django's own `dumpdata`/`loaddata` (engine-agnostic, no
`pg_dump` involved) rather than the production command:

1. `python manage.py dumpdata --exclude auth.permission --exclude
   contenttypes --exclude sessions.session --exclude admin.logentry` against
   the real `db.sqlite3` → 8 real rows across `accounts.user`,
   `axes.accessattempt`, `axes.accesslog`, `governance.auditlog`.
2. Built a fresh schema in a completely separate, throwaway SQLite file
   (`migrate --run-syncdb` against a different `DATABASE_URL`).
3. `loaddata` the dump into that throwaway database → "Installed 8
   object(s) from 1 fixture(s)".
4. Compared row counts table-by-table between the original and the
   restored database — all four tables matched exactly — and spot-checked
   the one real user row's actual field values (email/role/is_active),
   which matched byte-for-byte.
5. Deleted the throwaway database and dump file afterward.

This proves the underlying restore *concept* (a dump taken now, loaded
into an empty database, recovers the original data exactly) but is **not**
a substitute for testing the real command. Per the spec's own requirement
("do a real test restore once... not just files sitting there untested"):
**run steps 1-3 in the Restore procedure section above, for real, against
a staging Postgres database with real S3 credentials, before trusting this
in an actual emergency.**
