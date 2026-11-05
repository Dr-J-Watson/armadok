"""What an automatic update must remember between two runs, so an interrupted one is not lost."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from armadok.core.db import Migration

MIGRATION = Migration(
    "autoupdate-0001-state",
    """
    CREATE TABLE autoupdate_state (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        project TEXT NOT NULL,
        pending INTEGER NOT NULL DEFAULT 0,
        image_redeployed_at TEXT,
        PRIMARY KEY (node_id, project)
    );
    """,
)

# A redeploy caused only by a newer image happens at most once in this time: if a registry's
# digest never matches the local one (a mirror, a multi-platform index), the project is not
# pulled and recreated on every run.
IMAGE_COOLDOWN = timedelta(hours=24)


def _ensure(conn: sqlite3.Connection, node_id: str, project: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO autoupdate_state (node_id, project) VALUES (?, ?)",
        (node_id, project),
    )


def is_pending(conn: sqlite3.Connection, node_id: str, project: str) -> bool:
    """Say whether files were updated but the project was not redeployed with them yet."""
    row = conn.execute(
        "SELECT pending FROM autoupdate_state WHERE node_id = ? AND project = ?", (node_id, project)
    ).fetchone()
    return bool(row and row["pending"])


def set_pending(conn: sqlite3.Connection, node_id: str, project: str, pending: bool) -> None:
    """Remember (or forget) that a redeploy is owed for freshly pulled files."""
    _ensure(conn, node_id, project)
    conn.execute(
        "UPDATE autoupdate_state SET pending = ? WHERE node_id = ? AND project = ?",
        (int(pending), node_id, project),
    )
    conn.commit()


def image_cooldown_over(conn: sqlite3.Connection, node_id: str, project: str) -> bool:
    """Say whether an image-only redeploy is allowed again."""
    row = conn.execute(
        "SELECT image_redeployed_at FROM autoupdate_state WHERE node_id = ? AND project = ?",
        (node_id, project),
    ).fetchone()
    if not row or not row["image_redeployed_at"]:
        return True
    return datetime.now(UTC) - datetime.fromisoformat(row["image_redeployed_at"]) >= IMAGE_COOLDOWN


def mark_redeployed(conn: sqlite3.Connection, node_id: str, project: str, by_images: bool) -> None:
    """Record a redeploy: nothing is owed any more; an image-only one starts the cooldown."""
    _ensure(conn, node_id, project)
    conn.execute(
        "UPDATE autoupdate_state SET pending = 0,"
        " image_redeployed_at = CASE WHEN ? THEN ? ELSE image_redeployed_at END"
        " WHERE node_id = ? AND project = ?",
        (int(by_images), datetime.now(UTC).isoformat(), node_id, project),
    )
    conn.commit()
