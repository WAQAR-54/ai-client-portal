web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn -c deployment/gunicorn.conf.py config.wsgi:application
