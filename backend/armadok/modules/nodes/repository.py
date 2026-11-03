"""Database access for nodes. Rights and audit are handled by the routes, not here."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from armadok.core.db import Migration

MIGRATION = Migration(
    "nodes-0001-create",
    """
    CREATE TABLE nodes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('network', 'docker', 'full')),
        description TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    """,
)

_COLUMNS = "id, name, type, description, owner, created_at"


def list_nodes(conn: sqlite3.Connection) -> list[dict]:
    """Return all non-deleted nodes, oldest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM nodes WHERE deleted_at IS NULL ORDER BY created_at, id"
    )
    return [dict(r) for r in rows]


def get_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    """Return one non-deleted node, or None."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM nodes WHERE id = ? AND deleted_at IS NULL", (node_id,)
    ).fetchone()
    return dict(row) if row else None


def create_node(
    conn: sqlite3.Connection, name: str, node_type: str, description: str, owner: str
) -> dict:
    """Insert a node and return it."""
    node_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO nodes (id, name, type, description, owner, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, name, node_type, description, owner, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return get_node(conn, node_id)  # type: ignore[return-value]


def update_node(conn: sqlite3.Connection, node_id: str, changes: dict[str, str]) -> None:
    """Apply `changes` (name, type, description only) to a node."""
    allowed = {"name", "type", "description"}
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE nodes SET {assignments} WHERE id = ?", (*fields.values(), node_id))
    conn.commit()


def soft_delete_node(conn: sqlite3.Connection, node_id: str) -> None:
    """Mark a node deleted; it stays recoverable (purge after 30 days is a later task)."""
    conn.execute(
        "UPDATE nodes SET deleted_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), node_id)
    )
    conn.commit()
