# Operations guide

What is actually in place, how to check it, and what is *not* covered. Everything below was
observed on the running system unless it says otherwise.

## Deploying

`git push` to `main` runs `.github/workflows/ci.yml`:

1. **lint** - flake8, `black --check`, `pip-audit -r requirements.txt`.
2. **test** - fresh `pip install -r requirements.txt`, `manage.py check`, `makemigrations --check`,
   `manage.py test` (Postgres 16 service). Every gate is a plain command: the exit code decides.
   If the test step fails, a diagnostics step re-runs the suite and publishes the failing tests as
   annotations (readable without logging in to GitHub); it cannot change the outcome.
3. **deploy** (only if 1 and 2 passed, only on `main`) - SSH to the VPS: pre-deploy backup
   (best effort), `git pull`, `RELEASE_SHA=<commit> docker compose up -d --build`. The `web`
   container runs `migrate` and `collectstatic` before Gunicorn starts.
4. **health check** - `/healthz/` polled for ~50 s. Failure rolls the **code** back.
5. **informational, never a gate** (`continue-on-error`): smoke requests, then
   `manage.py ops_verify --annotate` inside the `web` container.

### Rollback limits
The rollback step resets Git to the previous commit and rebuilds. It does **not** undo a database
migration that already ran, so a migration the old code cannot read is not recoverable by it.
Migrations must stay additive (see `chat` 0023 and 0024) until a real down-migration plan exists.

## Checking production (read-only)

`docker compose exec -T web python manage.py ops_verify` reports migrations and schema drift,
Redis (PING + a 15-second throw-away key) and Celery (control ping), the beat schedule and how
long ago it last dispatched, provider connection state, one GET per news source, disk usage,
database connections, retention row counts, whether a backup target is configured, and the
`RELEASE_SHA` of the process. It writes nothing, deletes nothing and prints no credential or
user data. After every deploy the same output appears as annotations on the workflow run.

`docker compose exec -T worker printenv RELEASE_SHA` (and `beat`) confirms every container runs
the commit that was deployed; "git is current" says nothing about the image that is running.

## Health endpoints

| URL | Meaning |
|---|---|
| `/healthz/` | Database `SELECT 1`. 200 or 503. Polled by Docker and CI. |
| `/healthz/deep/` | Database + Redis (a real PING, 2 s bound). Anonymous, so it only returns fixed words. |

A 503 from either is logged as a WARNING, not an ERROR (`HealthProbeDowngradeFilter`), so a dependency
outage does not produce one alert per poll. A crash *inside* a probe is still an ERROR.

## Alerts

* Unhandled server errors go to **Sentry** (`SENTRY_DSN`) and to the admins in `ADMINS` by email,
  exactly once per error (one handler; falls back to a direct send if the broker is down).
  **If `ADMINS` is empty (it is, in production today) no email is sent and Sentry is the only alert.**
* Credentials are masked before they reach a log, an exception message or Sentry
  (`config/redaction.py`).
* There is no alert for: Celery worker stopped, beat stopped or stale, disk pressure, repeated
  provider failures. `ops_verify` shows these on demand; nothing pages anyone.

## Authorization

Roles are hierarchical - `user < manager < admin < superadmin` - and checked at the view
(`accounts/permissions.py`). Department scoping (an Admin sees their own department) and team
scoping (a Manager sees their own team) are enforced inside the views, per object. The full
route x role table is a test (`governance/test_authorization_matrix.py`) against a reviewed golden
file (`governance/authz_expected.json`): if a route's minimum role changes the test fails, and
after a deliberate change you rerun it with `AUTHZ_UPDATE=1` and review the diff.

Private files are never served from `/media/`; only `branding/` is public. Chat attachments are
served by `chat:download_attachment` (owner only), payment proofs by `billing:invoice_proof`.

## Files and retention

* Uploaded files are removed when the row that owns them is deleted (after the transaction commits).
  Deleting a conversation in the UI is a *soft* delete; the rows and files go when a department's
  retention period sweeps them, if one is set. The default is "forever".
* Audit log, email log and notifications have no automatic cleanup. `Delete email logs` is a manual
  SuperAdmin action.
* Docker container logs are capped at 3 x 10 MB per container. The app writes `logs/app.log`
  (5 MB x 3 rotated) **inside the container**, so it is replaced on every deploy.
* Docker build cache is not pruned automatically; run `docker builder prune` occasionally.

## Configuration worth knowing

| Variable | Effect |
|---|---|
| `RELEASE_SHA` | Set by the deploy step; shown by `ops_verify`. |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | Default to `FORCE_HTTPS`. Can be set alone: behind Cloudflare with an HTTP-only origin they can still be `True`. |
| `CELERY_TASK_TIME_LIMIT` / `CELERY_TASK_SOFT_TIME_LIMIT` | 1800 / 1500 seconds. A task that exceeds them is stopped. |
| `GUNICORN_WORKERS`, `GUNICORN_THREADS` | Default 3 x 4 = 12 concurrent requests. Every open chat reply holds one thread until it finishes. |
| `ADMINS` | `Name:email` pairs that receive crash emails. |

## Not covered (do not assume otherwise)

* No screen-reader testing has been done; only automated (axe) and keyboard checks.
* No staging environment; no load or concurrency test against production.
* No automatic fallback if a provider is down beyond trying the next candidate model.
* No down-migrations on rollback (see above).
