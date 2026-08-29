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

## Status: not yet live-tested end-to-end

I wrote and reviewed this command carefully, but **could not execute a
real test restore** from the environment I built it in — there's no
Postgres server or S3 credentials available there (this project's local
dev database is SQLite). Per the spec's own requirement ("do a real test
restore once... not just files sitting there untested"): **run steps 1–3
above for real against a staging database before trusting this in an
actual emergency.** Treat this document as the procedure, not yet as a
verified one.
