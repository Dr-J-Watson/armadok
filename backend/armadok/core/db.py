"""SQLite access and the migration runner.

Modules declare migrations as `Migration` objects; the runner applies the ones
not yet recorded, in a stable order, each in its own transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Migration:
    """One schema change.

    `id` must be unique and never change once released (e.g. "nodes-0001-create");
    `sql` is a script of one or more statements.
    """

    id: str
    sql: str


def connect(path: Path) -> sqlite3.Connection:
    """Open the database with foreign keys enforced and rows addressable by name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may run a request's dependency and handler on different
    # worker threads; a connection is still only ever used by one request at a time.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations: Iterable[Migration]) -> list[str]:
    """Apply pending migrations and return the ids that were applied.

    Applied ids are stored in `schema_migrations`, so running twice is a no-op.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY)")
    done = {row["id"] for row in conn.execute("SELECT id FROM schema_migrations")}
    applied: list[str] = []
    for migration in migrations:
        if migration.id in done:
            continue
        # executescript commits implicitly, so wrap the script and its record in one explicit
        # transaction to keep a failed migration from being half applied.
        conn.executescript(
            "BEGIN;\n"
            f"{migration.sql}\n"
            f"INSERT INTO schema_migrations (id) VALUES ('{migration.id}');\n"
            "COMMIT;"
        )
        applied.append(migration.id)
    return applied
