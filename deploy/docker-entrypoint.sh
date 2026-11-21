#!/bin/sh
# Container start: bring the database schema up to date, then run the given command (gunicorn).
# One web container per deployment, so migrating here cannot race another copy.
set -e

if [ "${ZEHNMS_MIGRATE_ON_START:-1}" = "1" ]; then
    python manage.py migrate --noinput
    python manage.py createcachetable
fi

exec "$@"
