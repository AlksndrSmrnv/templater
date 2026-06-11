#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_DSN:?DATABASE_DSN is required}"

# Wait for Postgres to accept connections by checking parsed libpq DSN.
python - <<'PY'
import asyncio
import os
import shlex
import sys
import time

import asyncpg

DSN_REQUIRED_KEYS = frozenset({"host", "port", "dbname", "user", "password"})


def parse_libpq_dsn(dsn: str) -> dict[str, str]:
    tokens = shlex.split(dsn, posix=True, comments=False)
    out: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"DATABASE_DSN: token without '=': {token!r}")
        key, _, value = token.partition("=")
        out[key.strip().lower()] = value

    missing = DSN_REQUIRED_KEYS - out.keys()
    if missing:
        raise ValueError(f"DATABASE_DSN: missing required keys: {sorted(missing)}")
    return out


parts = parse_libpq_dsn(os.environ["DATABASE_DSN"])
host = parts.get("hostaddr") or parts["host"]
port = int(parts["port"])
user = parts["user"]
password = parts["password"]
database = parts["dbname"]

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

exec "$@"
