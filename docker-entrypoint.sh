#!/bin/sh
set -e

mkdir -p "${DATA_DIR:-/data}"

echo "[entrypoint] applying database migrations"
alembic upgrade head

exec "$@"
