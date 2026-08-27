release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn -c deployment/gunicorn.conf.py config.wsgi:application
