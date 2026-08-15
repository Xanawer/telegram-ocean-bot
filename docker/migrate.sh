#!/bin/sh
set -eu

exec /app/.venv/bin/alembic upgrade head
