"""Storage of a node's own settings (one row per setting that differs from the default)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from armadok.core.db import Migration

MIGRATION = Migration(
    "nodeconfig-0001-settings",
    """
    CREATE TABLE node_settings (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (node_id, key)
    );
    """,
)


def all_for(conn: sqlite3.Connection, node_id: str) -> dict[str, str]:
    """Return every setting kept for a node."""
    rows = conn.execute("SELECT key, value FROM node_settings WHERE node_id = ?", (node_id,))
    return {r["key"]: r["value"] for r in rows}


def save(conn: sqlite3.Connection, node_id: str, changes: dict[str, str], by: str) -> None:
    """Keep the changed settings; an empty value puts a setting back to its default.

    The caller commits.
    """
    now = datetime.now(UTC).isoformat()
    for key, value in changes.items():
        if value == "":
            conn.execute("DELETE FROM node_settings WHERE node_id = ? AND key = ?", (node_id, key))
        else:
            conn.execute(
                "INSERT INTO node_settings (node_id, key, value, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id, key) DO UPDATE SET"
                " value = excluded.value, updated_by = excluded.updated_by,"
                " updated_at = excluded.updated_at",
                (node_id, key, value, by, now),
            )
