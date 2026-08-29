#!/bin/sh
# Single entrypoint for every service in docker-compose.yml (web, worker,
# beat) so they all run from exactly the same image - no drift between what
# the web process imports and what the worker/beat processes import.
#
# Only the "web" role runs migrate/collectstatic, so multiple replicas or
# the worker/beat containers starting in any order never race each other
# applying migrations.
set -e

case "$1" in
  web)
    echo "Running migrations..."
    python manage.py migrate --noinput
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
    exec gunicorn -c deployment/gunicorn.conf.py config.wsgi:application
    ;;
  worker)
    exec celery -A config worker -l info
    ;;
  beat)
    exec celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;
  *)
    exec "$@"
    ;;
esac
