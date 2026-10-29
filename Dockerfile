# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PANEL_DB_PATH=/data/panel.sqlite3

WORKDIR /app
COPY backup_panel/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup_panel/ .
# whitenoise serves these; the key below is only for this build step
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

# a fixed unprivileged user; /data (the SQLite database) is the only place it writes
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin panel \
 && mkdir /data && chown panel /data
COPY --chmod=0755 docker/entrypoint.sh /entrypoint.sh
USER panel
VOLUME ["/data"]
EXPOSE 8000
# A TCP check on purpose: an http request would be redirected to https (TRUSTED_PROXY_COUNT) or refused for its
# Host header (DJANGO_ALLOWED_HOSTS). gunicorn only listens once the app has loaded, so this is enough.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
