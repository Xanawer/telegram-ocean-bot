#!/bin/sh
set -eu

exec uv run alembic upgrade head
