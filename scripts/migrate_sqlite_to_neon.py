"""Migrate the local SQLite history database into Neon PostgreSQL.

Usage from the SmartHome project root:
    python scripts/migrate_sqlite_to_neon.py

Set DATABASE_URL in the environment first. The migration is explicit and
idempotent for existing event/state IDs.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database


if __name__ == "__main__":
    url = os.getenv("DATABASE_URL", "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit(
            "DATABASE_URL must contain the Neon PostgreSQL connection string."
        )

    result = database.migrate_sqlite_to_postgres(
        sqlite_path=database.DB_PATH,
        postgres_url=url,
    )
    print(
        f"Migration complete: {result['events']} event(s), "
        f"{result['states']} state snapshot(s)."
    )
