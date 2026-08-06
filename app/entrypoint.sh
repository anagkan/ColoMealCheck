#!/bin/sh
set -e

echo "[entrypoint] waiting for database..."
python -m app.wait_for_db

echo "[entrypoint] running migrations..."
alembic upgrade head

echo "[entrypoint] seeding defaults..."
python -m app.seed

echo "[entrypoint] starting uvicorn on 0.0.0.0:8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips '*'
