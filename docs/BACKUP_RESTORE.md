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

Production actually runs on a self-managed VPS via Docker Compose over SSH
(`.github/workflows/ci.yml`'s deploy job), not Railway — despite the
Railway-specific scheduling instructions further down, written for an
earlier hosting plan and left as-is below since the underlying cron-job
mechanics still apply to whatever host actually runs a recurring job. Set
these in the **server's own `.env`** (the same file `docker-compose.yml`'s
`env_file:` already reads for the `web`/`worker`/`beat` services), not in
this repo's `.env.example`, which only documents the variable names:

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

## It now also runs automatically before every deploy

`.github/workflows/ci.yml`'s deploy job runs `docker compose exec -T web
python manage.py backup_database` against the *currently running* (pre-
deploy) container before pulling new code or applying any migration —
see that file's "Deploy over SSH" step. This is best-effort: if
`BACKUP_S3_BUCKET` isn't set yet, the command exits non-zero and the
deploy script logs a warning to `~/ai-client-portal/deploy.log` and
carries on rather than blocking the deploy. Once the variables below are
actually set on the server, this pre-deploy backup starts working for
real with no further changes needed.

This does not replace a recurring schedule below — it only guarantees a
fresh backup exists right before the riskiest moment (a new migration
running), not one at a predictable time of day regardless of deploys.

## Recurring schedule — now automatic via Celery Beat

This used to require someone to separately configure a Railway Cron Job
or a VPS crontab entry — easy to forget, and invisible if it was never
actually set up. It's now a regular Celery Beat `PeriodicTask`, seeded by
`accounts/migrations/0013_seed_daily_backup_schedule.py` exactly like
this app's other 5 scheduled jobs (invoice sweep, trial-expiry sweep,
etc.) — no separate cron service to remember, running on the same
`worker`/`beat` containers `docker-compose.yml` already brings up.

- **Task**: `accounts.tasks.run_scheduled_database_backup` (thin wrapper
  around the same `backup_database` command, logging — not raising — a
  `CommandError` when `BACKUP_S3_BUCKET` isn't set yet, so a not-yet-
  configured bucket never shows up as a failed/retried Celery task).
- **Schedule**: daily at 03:00 UTC, ahead of every other scheduled job in
  this app (04:00/04:30/05:00), so a fresh backup exists before any of
  them run.
- Editable afterwards from Django admin → Periodic Tasks → "Daily
  database backup", same as any other scheduled job here — no code
  change needed to move the time.

Until `BACKUP_S3_BUCKET`/`BACKUP_S3_ACCESS_KEY_ID`/etc. are actually set
on the server, this task runs on schedule but does nothing (logs a
warning and exits) — setting those variables is still a required manual
step; only the scheduling itself is now automatic.

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

**The real `pg_dump`/`pg_restore` path was tested for real against
production, 2026-09-19.** Directly on the production VPS (SSH, see
`docs/PRODUCTION_ACCESS.md`), against the actual live `ai_client_portal`
Postgres database - not a substitute, not a different engine:

1. `pg_dump "$DATABASE_URL" --format=custom --file=/tmp/restore-test.dump`
   run inside the running `web` container - the exact command/flags
   `backup_database.py::_dump()` uses - against the real production
   database. Produced a 292 KB custom-format dump.
2. `CREATE DATABASE ai_client_portal_restore_test` on the **same** Postgres
   instance - a second, throwaway database, never the real one.
3. `pg_restore --dbname ... --no-owner` into that throwaway database. One
   harmless warning surfaced and is worth knowing about: the container's
   `pg_dump`/`pg_restore` client is 17.11 (current Debian default). Server is
   `postgres:16.15` - client newer than server. The dump's restore preamble
   included `SET transaction_timeout = 0;` (a directive that only exists on
   Postgres 17+ servers), which the 16 server didn't recognize -
   `pg_restore` logged "errors ignored on restore: 1" and continued; exit
   code 0. Optional cleanup: pin `postgresql-client-16` in the `Dockerfile`
   to match the server exactly and silence this, though it did not affect
   the restored data at all (confirmed by step 4).
4. Compared row counts between the live database and the restored copy
   across `accounts_user`, `chat_conversation`, `chat_message`, and
   `billing_invoice` - **all four matched exactly** (7/21/137/2). Spot-checked
   three real user rows' actual field values (id/email/role/is_active/
   date_joined) between the live database and the restored copy -
   **byte-for-byte identical**. (Not reproduced here since this repo is
   public and those are real users' email addresses - re-run the same
   check yourself if you need to see it again.)
5. Dropped the throwaway database and deleted every temp file (both the
   container's own `/tmp` and the VPS host's `/tmp`) immediately after.
   Production itself was never written to at any point - confirmed
   `accounts_user` count unchanged (7) and all 5 containers still healthy
   afterward.

**This is now a real, verified recovery time**: dump 292 KB in a few
seconds, restore in a few seconds, at today's data volume. Re-run this
whenever data volume grows enough that the timing might meaningfully
change, and once `BACKUP_S3_BUCKET`/etc. are actually set (see below) -
this test exercised the dump/restore mechanics directly, not the S3
upload/download leg, since those variables aren't configured on the
server yet.

**One real gap this surfaced**: `BACKUP_S3_BUCKET` (and the other
`BACKUP_S3_*` variables) are **not set on the production server**. The
daily Celery Beat task and the pre-deploy backup step have been running on
schedule this whole time but doing nothing for real (logging a warning and
exiting, exactly as designed to fail-open rather than block deploys) -
**no actual off-server backup has ever been created yet.** Setting those
variables on the server is the one remaining step between "the mechanism
works" (now proven above) and "backups are actually happening."

**Earlier gap, already fixed**: the production Docker image installed
`libpq5` (the client *library*, for psycopg2) but never `postgresql-client`
(the package that actually provides the `pg_dump` binary) - so
`backup_database` would have failed with "command not found" the very
first time anything tried to run it, pre-deploy backup included. Fixed in
the `Dockerfile`'s runtime stage.

**What was tested earlier, 2026-08-30, before Postgres/S3 access existed**:
a logical backup/restore cycle against SQLite dev data via Django's own
`dumpdata`/`loaddata` - superseded by the real test above, kept here for
the record. 8 rows across 4 tables, restored into a fresh throwaway SQLite
file, row counts and one user row's fields matched exactly, then deleted.
