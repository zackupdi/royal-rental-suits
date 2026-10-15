#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --noinput

if [[ -n "${BOOTSTRAP_SUPERUSER_USERNAME:-}" || -n "${BOOTSTRAP_SUPERUSER_PASSWORD:-}" ]]; then
    python manage.py bootstrap_superuser
fi
