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
  provider failures. Nothing pages anyone. What exists is visibility: the SuperAdmin dashboard's
  System status shows each background task's recorded outcome (last success or failure with the
  exception class only, duration, retries, running, overdue) and flags a stale scheduler;
  `ops_verify` prints the same plus today's AI call counters, browser-side errors, streaming
  capacity and media orphans. Outcomes and counters are kept in the shared cache (Redis), so a
  Redis flush resets them and a task with no record shows "No outcome recorded", never "OK".

## Authorization

Roles are hierarchical - `user < manager < admin < superadmin` - and checked at the view
(`accounts/permissions.py`). Department scoping (an Admin sees their own department) and team
scoping (a Manager sees their own team) are enforced inside the views, per object. The full
route x role table is a test (`governance/test_authorization_matrix.py`) against a reviewed golden
file (`governance/authz_expected.json`): if a route's minimum role changes the test fails, and
after a deliberate change you rerun it with `AUTHZ_UPDATE=1` and review the diff.

Private files are never served from `/media/`; only `branding/` is public. Chat attachments are
served by `chat:download_attachment` (owner only), payment proofs by `billing:invoice_proof`.
The SuperAdmin "Server Media" page (`governance/media_views.py`) is not a second door: it is
SuperAdmin-only, addresses a file by (source, database id) and never by path, sends every download
as an opaque attachment, previews only images/PDF/plain text whose first bytes match their
extension (never SVG or HTML), audits each download and preview of a private file, and can delete
only a file that no record refers to. Its "View" page shows a file and its facts without ever making
it public; anything it cannot show safely says "Preview not available" and offers a download.

Object-level rules (`governance/test_object_authorization.py`): a department Admin manages the Users
and Managers of their own department only - not another department, a peer Admin or a SuperAdmin -
and an Admin with no department has no scope at all. Chat conversations are owner-only for every
role, SuperAdmin included.

## Rate limits and Redis

Limits are counted in Redis. What happens when Redis is unreachable depends on the kind of limit
(`accounts/rate_limit.py`):

| Kind | Used for | During a Redis outage |
|---|---|---|
| SECURITY_CRITICAL | login, signup, password reset, Google sign-in | per-process in-memory counter with the same limit (3 Gunicorn workers => at most ~3x the limit, bounded) |
| EXPENSIVE | per-minute AI message limit | same local fallback, so paid provider calls stay restricted |
| NORMAL | everything else (the browser-error beacon) | fails open |

Nothing fails closed: a limiter that cannot count never blocks someone it has not counted itself.
Daily/monthly plan quotas are database-backed and unaffected. Login is throttled three ways:
one username (django-axes locks the account after 5 failures, from any IP), many usernames from
one IP (failed logins are counted per IP: `LOGIN_IP_FAILURE_LIMIT`, default 30 per hour; successes
never count, so an office signing in each morning is unaffected) and one username from many IPs
(the same axes lock). The refusal text does not depend on whether the account exists.
**Limit:** the client IP is read from `CF-Connecting-IP` (then `X-Forwarded-For`); that header is
trustworthy only if the origin accepts traffic from Cloudflare alone. Someone who reaches the
origin directly can send any value and dodge the per-IP limits.

## Capacity (Gunicorn, streaming)

Default 3 workers x 4 threads = 12 requests at once, and every open chat reply holds one thread for
its whole duration. `ops_verify` (section `capacity`) reports how many replies are streaming right
now against that number. There is no load test behind the default, so it is an assumption, not a
measurement: raise `GUNICORN_THREADS` only when `capacity` shows streaming at or above ~75% of the
threads at peak. Each request also opens its own database connection (`CONN_MAX_AGE=0`) and the
database allows 100, so 12 -> 24 threads is comfortable on that axis.

## Context, attachments and quotas

A chat request sends a bounded slice of the conversation (`chat/context_window.py`), not all of it:
the current message, the newest messages that fit, the opening message, and short notes on older
questions - inside min(plan `max_context_tokens`, model window minus an 8192-token reply reserve).
Model windows are configured assumptions (`MODEL_CONTEXT_TOKENS*`), not read from the providers, so
they are unverified against the real limits. Extracted attachment text is cached (Redis, 6 h, keyed
by message and file state); a replaced file is read again. The daily new-conversation limit is
enforced atomically under a row lock on the user, and a deleted conversation still counts.

## Database timeouts

Production reports `statement_timeout=0` and `idle_in_transaction_session_timeout=0`. Both are
opt-in via `DB_STATEMENT_TIMEOUT_MS` and `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` (default 0 = unchanged;
rationale, risk and rollback are in `config/db_options.py`). Start with idle-in-transaction; a
statement timeout also applies to migrations, the retention sweep and exports.

## Cache review

| Cache | Lifetime | Invalidation | Note |
|---|---|---|---|
| AI response cache (`chat/response_cache.py`) | 1 h | key = user + model + system prompt + full history, so any change to the prompt, history or an attachment is a new key; nothing purges it | identical prompts can return an up-to-1-hour-old answer; never holds the "older messages condensed" note; truncated replies are not cached; cache hits are not counted as provider calls |
| Live Intelligence feeds | 15 min | manual refresh, 1 per user per minute | grounding text is part of the prompt, so a refreshed feed changes the response-cache key |
| Attachment text | 6 h (failure 10 min) | key includes file size and mtime | |
| Media scan (Server Media) | 10 min | rescan button (30 s cooldown) and every delete | the file LIST is database-driven and always current; only the totals/orphans are cached |
| Pricing, provider/model visibility, usage, System status | not cached | - | recomputed per request |
| Celery results | 1 day (Celery default `result_expires`) | expire by themselves | |
| Task outcomes, AI counters | 30 d / 8 d | expire by themselves | diagnostics, not accounting |

Redis in `docker-compose.yml` has no `maxmemory` or eviction policy: every key above expires, but a
key written without a TTL would never leave. None of the new keys is written without one.

## Files and retention

* Uploaded files are removed when the row that owns them is deleted (after the transaction commits).
  Deleting a conversation in the UI is a *soft* delete; the rows and files go when a department's
  retention period sweeps them, if one is set. The default is "forever".
* Audit log, email log and notifications have no automatic cleanup. `Delete email logs` is a manual
  SuperAdmin action.
* Current retention, per data set (nothing else is deleted automatically, and **no cleanup job was
  added: no explicit retention policy exists to base one on**):

  | Data | Kept | Removed by |
  |---|---|---|
  | Audit log | forever | nothing; the model refuses updates and deletes |
  | Email log | forever | SuperAdmin "Delete email logs" (manual) |
  | Notifications | forever | nothing |
  | Conversations, messages, attachments | forever unless the department sets a retention period | daily sweep for those departments; files go with their rows |
  | Temporary files | only `backup_database`'s temp directory | removed when the command ends |
  | Celery results | 1 day | expire by themselves |
  | Cache keys | see the cache review | expire by themselves |
  | Database backups | `BACKUP_RETENTION_DAYS` (30) in the S3 bucket | the backup command - **but no bucket is configured in production, so no backup exists** |

  `ops_verify` prints row counts and the oldest row of each so growth is visible.
* Storage visibility: `ops_verify` (`disk`) reports disk usage, the media directory's size, and the
  count of orphan candidates; the SuperAdmin "Server Media" page shows the media disk's
  NORMAL/WARNING/CRITICAL state (`MEDIA_DISK_WARN_PCT`/`MEDIA_DISK_CRITICAL_PCT`) and lists files no
  record refers to. Database size and largest tables are in `ops_verify` (`database`); Docker's own
  disk use is not visible from inside a container - run `docker system df` on the host. Nothing is
  ever deleted automatically; an orphan is deleted one at a time, by a SuperAdmin, after a "Delete
  file?" confirmation and - the part that matters - a fresh server-side check that nothing refers to
  it (a file that became referenced after the page loaded is refused).
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
| `LOGIN_IP_FAILURE_LIMIT` | Failed logins per IP per hour across all usernames (default 30). |
| `MODEL_CONTEXT_TOKENS_DEFAULT`, `MODEL_CONTEXT_TOKENS`, `MODEL_CONTEXT_TOKENS_BY_MODEL` | Input budget per model (default 32000; adapter defaults in `config/settings.py`). |
| `MEDIA_DISK_WARN_PCT`, `MEDIA_DISK_CRITICAL_PCT` | Server Media disk thresholds (80 / 90). |
| `MEDIA_MEDIUM_MIN_BYTES`, `MEDIA_LARGE_MIN_BYTES`, `MEDIA_LARGE_THRESHOLDS_MB` | Server Media size filter: Small/Medium/Large limits (1 MB / 10 MB) and the extra large-file thresholds (50, 100, 500 MB). Visibility only - nothing is deleted by size. |
| `DB_STATEMENT_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | Opt-in PostgreSQL timeouts (default off). |

Every variable the code reads is listed in `.env.example` (a test fails otherwise); names only,
never values, are checked.

## Not covered (do not assume otherwise)

* No screen-reader testing has been done; only automated (axe) and keyboard checks.
* No staging environment; no load or concurrency test against production.
* No automatic fallback if a provider is down beyond trying the next candidate model.
* No down-migrations on rollback (see above).
