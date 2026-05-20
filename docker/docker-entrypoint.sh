#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"

# Wait for Postgres to accept connections by checking the asyncpg URL.
python - <<'PY'
import asyncio
import os
import sys
import time

from urllib.parse import urlsplit

import asyncpg

url = os.environ["DATABASE_URL"]
# Strip SQLAlchemy driver part for asyncpg.connect()
parts = urlsplit(url)
host = parts.hostname or "localhost"
port = parts.port or 5432
user = parts.username or ""
password = parts.password or ""
database = (parts.path or "").lstrip("/") or "postgres"

deadline = time.time() + 60
last_err: Exception | None = None

async def wait():
    global last_err
    while time.time() < deadline:
        try:
            conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database=database)
            await conn.close()
            print("Postgres is ready")
            return
        except Exception as exc:
            last_err = exc
            await asyncio.sleep(1.0)
    print(f"Postgres not reachable: {last_err}", file=sys.stderr)
    sys.exit(1)

asyncio.run(wait())
PY

echo "Applying Alembic migrations..."
alembic upgrade head

if [ "${SEED_REFERENCE_DATA:-1}" = "1" ]; then
    echo "Seeding reference data (idempotent)..."
    python -m scripts.seed_reference_data || echo "Seeding failed (continuing)"
fi

exec "$@"
