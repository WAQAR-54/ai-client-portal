# syntax=docker/dockerfile:1

# ---- Stage 1: build dependencies -------------------------------------------
# Keeps compilers and build headers (needed for a couple of wheels to build
# on some architectures) out of the final image entirely.
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim AS runtime

# libpq5 is the runtime-only counterpart of libpq-dev above (psycopg2 needs
# it to actually connect to Postgres, not just to build). postgresql-client
# provides pg_dump, which accounts/management/commands/backup_database.py
# shells out to - without it, every backup attempt (daily or pre-deploy,
# see .github/workflows/ci.yml) fails with "command not found". Debian's
# bookworm repo ships postgresql-client-15 by default, one major version
# behind the docker-compose db service's postgres:16 - fine for a plain
# pg_dump of this schema, but worth knowing if this image ever needs a
# PG16-specific dump feature (would need the PGDG apt repo instead).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

RUN chmod +x /app/docker-entrypoint.sh && \
    mkdir -p /app/logs /app/media /app/staticfiles && \
    chown -R appuser:appuser /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings

USER appuser

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["web"]
