#!/bin/sh
# web    - apply database migrations, then serve with gunicorn
# worker - wait until the web container has migrated, then run backup/restore jobs
# other  - run it as is (e.g. `createsuperuser`)
set -eu
case "${1:-web}" in
  web)
    python manage.py migrate --noinput
    exec gunicorn config.wsgi --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-2}" --threads "${GUNICORN_THREADS:-4}" \
      --timeout 120 --worker-tmp-dir /dev/shm --access-logfile - --forwarded-allow-ips="${GUNICORN_FORWARDED_ALLOW_IPS:-*}"
    ;;
  worker)
    until python manage.py migrate --check >/dev/null 2>&1; do
      echo "waiting for the database to be migrated by the web container..."
      sleep 2
    done
    exec python manage.py run_jobs
    ;;
  *)
    exec "$@"
    ;;
esac
