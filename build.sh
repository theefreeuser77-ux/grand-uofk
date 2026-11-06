#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate

# Free Render plans have no Shell. To create the first admin account, set
# DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD in the service's environment.
# If the account already exists this step is skipped.
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py createsuperuser --noinput --email "${DJANGO_SUPERUSER_EMAIL:-admin@example.com}" \
    || echo "Admin account $DJANGO_SUPERUSER_USERNAME already exists; skipped."
fi
