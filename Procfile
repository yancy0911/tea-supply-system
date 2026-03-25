web: bash -c 'python -c "import os; print(\"DATABASE_URL_SET=\", bool(os.getenv(\"DATABASE_URL\")))" && python manage.py migrate --noinput && exec gunicorn tea_supply.wsgi:application --bind 0.0.0.0:${PORT} --workers 2 --timeout 120'

