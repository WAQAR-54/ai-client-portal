# Secrets management audit

Audit performed 2026-09-19 as Part 6 of the security/reliability/testing
hardening pass. Scope: hardcoded secrets in code/git history, env-var-based
loading, `.env` gitignore status, `FIELD_ENCRYPTION_KEY` backup, GitHub repo
visibility/collaborator access.

## Hardcoded secrets — none found

Scanned:
- The full git history (205 commits, `git log -p --all`) for AWS keys,
  Anthropic/OpenAI key prefixes (`sk-ant-`, `sk-proj-`), private key headers,
  Slack tokens, Google API keys, and hardcoded `SECRET_KEY`/
  `FIELD_ENCRYPTION_KEY`/`*_API_KEY`/`*_TOKEN`/`*_SECRET`/`*_PASSWORD` literal
  assignments outside `env()` calls.
- The current working tree with [`detect-secrets`](https://github.com/Yelp/detect-secrets)
  (all built-in plugins), excluding `venv/`, `media/`, `staticfiles/`.

**Result: zero real secrets in either.** `detect-secrets` flagged 21 lines
across 10 files — every one is a false positive, confirmed by hand:
- The fixed test password `"pw12345!"` (and a few one-off fixture values
  like `"supersecret"`, `"a-brand-new-strong-pw9"`) used throughout the test
  suite — fake, in-memory/SQLite test data only.
- `ci.yml`'s `SECRET_KEY: ci-test-secret-key` and the Postgres service
  container's `POSTGRES_PASSWORD: postgres` — CI-only, ephemeral, never
  reachable outside the GitHub-hosted runner.
- `_INSECURE_DEFAULT_KEY`/`_INSECURE_DEFAULT_FIELD_ENCRYPTION_KEY` in
  `config/settings.py` — intentional, documented dev-only fallback constants,
  already guarded by a startup check that refuses to run with `DEBUG=False`
  while either is still set to its default value.
- Placeholder values inside comments/`.env.example`/docs showing the
  *format* of a credential (`postgres://user:pass@host/db`), not a real one.

## Env-var-based loading — confirmed

Every real secret (`SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `DATABASE_URL`,
`EMAIL_HOST_PASSWORD`, `SENTRY_DSN`, `BACKUP_S3_SECRET_ACCESS_KEY`, AI
provider keys, etc.) loads via `django-environ`'s `env(...)` in
`config/settings.py`, never a literal in code. AI provider API keys
specifically aren't env vars at all — they're entered through the admin UI
and stored encrypted in the database (`Provider.api_key_encrypted`), which is
what `FIELD_ENCRYPTION_KEY` below actually protects.

## `.env` gitignored — confirmed, never committed

`.env` is listed in `.gitignore` and `git log --all --full-history -- .env`
returns nothing — it has never existed in this repo's history, not just
currently ignored.

## `FIELD_ENCRYPTION_KEY` backup — action needed, outside this session

This key decrypts every connected AI provider's stored API key
(`Provider.api_key_encrypted`). Losing it — a dead VPS, a wiped disk, a
server `.env` that was never backed up anywhere else — makes those fields
**permanently** unreadable. Unlike `SECRET_KEY` (rotatable any time) or a
database password (resettable), there is no recovery path for this one.

This audit can confirm the *code's* handling of it (loaded from env, has a
guarded dev-only default, never logged or hardcoded) but **cannot confirm
whether a real backup of the production value exists** — that lives only on
the server's own `.env` and whatever the user has (or hasn't) copied
elsewhere. Added a callout to the README; the actual backup is a manual step
for whoever holds the production server's `.env`:

```bash
grep FIELD_ENCRYPTION_KEY /path/to/production/.env
```
— copy that value into a password manager or secrets vault that isn't the
production server itself, today, before it's needed.

## GitHub repo visibility — flagged, public

`GET /repos/WAQAR-54/ai-client-portal` (unauthenticated) returns
`"private": false, "visibility": "public"`. This is a commercial SaaS
product with paying-client billing/invoice data in its database (though, per
the audit above, no actual secrets in the *repository* itself) — worth a
deliberate decision on whether that's intended, since it wasn't something
this audit could infer on its own.

## Collaborator access — not checked from this session

Listing a repo's collaborators requires an authenticated GitHub session with
admin rights on it; this session has no `gh` CLI auth and no GitHub token
available. Check directly: repo → Settings → Collaborators and teams.
