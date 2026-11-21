# syntax=docker/dockerfile:1
# ZehnMS production image: gunicorn + WhiteNoise, run by compose.yaml next to PostgreSQL.
# See docs/DEPLOYMENT.md, "VPS with Docker".
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# postgresql-client: pg_dump / pg_restore for the in-app backups (Settings > Backups).
# Debian's client is 17, matching the postgres:17 service in compose.yaml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 zehnms \
    && useradd --system --uid 10001 --gid zehnms --home-dir /app --shell /usr/sbin/nologin zehnms

WORKDIR /app

COPY requirements.txt requirements-server.txt ./
RUN pip install -r requirements-server.txt

COPY . .

# Static files are collected into the image (WhiteNoise serves them with hashed names).
# The key below exists only for this build step; the real one comes from the server's .env.
RUN DJANGO_SECRET_KEY=collectstatic-only DJANGO_DEBUG=false DJANGO_FILE_LOGGING=false \
        ZEHNMS_DATA_DIR=/tmp/build python manage.py collectstatic --noinput --verbosity 0 \
    && rm -rf /tmp/build \
    && install -d -o zehnms -g zehnms -m 750 /data /data/media /data/backups /data/logs

USER zehnms

ENV DEPLOYMENT_MODE=cloud \
    ZEHNMS_DATA_DIR=/data \
    DJANGO_FILE_LOGGING=false \
    GUNICORN_BIND=0.0.0.0:8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "/app/deploy/healthcheck.py"]

ENTRYPOINT ["sh", "/app/deploy/docker-entrypoint.sh"]
CMD ["gunicorn", "--config", "/app/deploy/gunicorn.conf.py", "possumus.wsgi:application"]
