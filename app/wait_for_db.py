"""Block until Postgres accepts connections, so the entrypoint can migrate."""
from __future__ import annotations

import sys
import time

from sqlalchemy import text

from app.db import engine

DEADLINE_SECONDS = 60


def main() -> int:
    started = time.monotonic()
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return 0
        except Exception as exc:  # noqa: BLE001 - any driver error means "not yet"
            if time.monotonic() - started > DEADLINE_SECONDS:
                print(f"[wait_for_db] giving up after {DEADLINE_SECONDS}s: {exc}", file=sys.stderr)
                return 1
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
