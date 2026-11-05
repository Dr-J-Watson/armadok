"""Storage of each project's terminal settings (off until someone turns it on)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "terminal-0001-create",
    """
    CREATE TABLE terminal_settings (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        project TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        writable INTEGER NOT NULL DEFAULT 0,
        network INTEGER NOT NULL DEFAULT 0,
        image TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (node_id, project)
    );
    """,
)

DEFAULT_IMAGE = "alpine:3.20"


def get(conn: sqlite3.Connection, node_id: str, project: str) -> dict[str, Any]:
    """Return a project's settings; a project nobody configured has the terminal off."""
    row = conn.execute(
        "SELECT enabled, writable, network, image FROM terminal_settings"
        " WHERE node_id = ? AND project = ?",
        (node_id, project),
    ).fetchone()
    if row is None:
        return {"enabled": False, "writable": False, "network": False, "image": DEFAULT_IMAGE}
    return {
        "enabled": bool(row["enabled"]),
        "writable": bool(row["writable"]),
        "network": bool(row["network"]),
        "image": row["image"],
    }


def save(
    conn: sqlite3.Connection, node_id: str, project: str, settings: dict[str, Any], by: str
) -> None:
    """Store a project's settings."""
    conn.execute(
        "INSERT INTO terminal_settings (node_id, project, enabled, writable, network, image,"
        " updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(node_id, project) DO UPDATE SET enabled = excluded.enabled,"
        " writable = excluded.writable, network = excluded.network, image = excluded.image,"
        " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (
            node_id,
            project,
            int(settings["enabled"]),
            int(settings["writable"]),
            int(settings["network"]),
            settings["image"],
            by,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
