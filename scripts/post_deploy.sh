#!/usr/bin/env bash
# Run on the API host after pulling new code.
# Usage (from server/):  bash scripts/post_deploy.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Done. Restart the Django/gunicorn service if it is already running."
