# AI Client Portal

## Running with Docker Compose

Brings up the full stack: Django (via Gunicorn), PostgreSQL, Redis, a Celery
worker, and Celery beat (for the scheduled notification sweep).

### Prerequisites

- Docker + Docker Compose v2 (`docker compose version`)
- A `.env` file in the project root — copy `.env.example` to `.env`. Every
  value has a safe local default.

### First run (clean checkout)

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

That's it — migrations and `collectstatic` run automatically every time the
`web` container starts (see `docker-entrypoint.sh`), so there's no separate
migrate step to remember. The app is at http://localhost:8000/.

Chat won't actually work until you connect at least one AI provider: log in
as the superuser, go to Admin → Providers, and paste in a real API key —
keys are stored encrypted in the database, not in `.env`.

Optional: seed demo accounts (admin/manager/user) instead of a lone
superuser:

```sh
docker compose exec web python manage.py create_demo_users
```

### Everyday commands

```sh
docker compose up -d          # start everything in the background
docker compose logs -f web    # tail the Django/Gunicorn logs
docker compose logs -f worker # tail Celery task logs
docker compose exec web python manage.py <any management command>
docker compose down           # stop everything (data volumes are kept)
docker compose down -v        # stop AND wipe Postgres/Redis/media data
```

After pulling code changes that touch `requirements.txt` or the Dockerfile,
rebuild the image:

```sh
docker compose up -d --build
```

### What each service does

| Service  | Runs                                             |
|----------|---------------------------------------------------|
| `db`     | PostgreSQL 16                                      |
| `redis`  | Redis 7 — Celery broker/result backend             |
| `web`    | `migrate` + `collectstatic` + Gunicorn (the app)   |
| `worker` | Celery worker (async tasks: emails, etc.)          |
| `beat`   | Celery beat (the daily trial-expiry sweep, DB-scheduled) |

All four app containers build from the same image (`Dockerfile`) — only the
startup command differs, selected via `docker-entrypoint.sh web|worker|beat`.

### Secrets and configuration

- Nothing secret is baked into the image. `web`/`worker`/`beat` read
  `SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `SENTRY_DSN`, etc. from `.env` at
  container **start** time via `env_file:` in `docker-compose.yml` — `.env`
  itself is gitignored and is never copied into the image (see
  `.dockerignore`). AI provider API keys are not part of this at all — they
  live encrypted in the database, connected from Admin → Providers.
- `DATABASE_URL` and `REDIS_URL` are overridden in `docker-compose.yml` to
  point at the bundled `db`/`redis` containers, regardless of what `.env` has
  for native (non-Docker) local dev.
- Uploaded chat attachments persist in the `media_data` named volume across
  container restarts/rebuilds.

### Going to production with this compose file

The defaults (`DEBUG=True`, a placeholder `SECRET_KEY`, a placeholder
Postgres password) are meant for local evaluation only. Before using this
anywhere real:

1. Set a real, random `SECRET_KEY` in `.env`.
2. Set `DEBUG=False` in `.env`.
3. Set `POSTGRES_PASSWORD` in `.env` to something real (it's read by both the
   `db` service and the `DATABASE_URL` built for the app containers).
4. Review `ALLOWED_HOSTS` for your actual domain.

(For a bare-VPS deployment without Docker, see `deployment/` instead —
`gunicorn.conf.py`, `nginx.conf.example`, and a systemd unit file. Railway
deploys use `railway.json` and don't need any of this.)

### Database backups

See [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md).
