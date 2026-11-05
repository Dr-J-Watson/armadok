"""Storage of the latest comparison for each image of a machine."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "imageupdates-0001-state",
    """
    CREATE TABLE image_updates (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        ref TEXT NOT NULL,
        state TEXT NOT NULL,
        in_use INTEGER NOT NULL DEFAULT 0,
        local_digests TEXT NOT NULL DEFAULT '[]',
        remote_digest TEXT,
        error TEXT,
        checked_at TEXT NOT NULL,
        notified_digest TEXT,
        PRIMARY KEY (node_id, ref)
    );
    """,
)

LOGINS_MIGRATION = Migration(
    "imageupdates-0002-logins",
    """
    CREATE TABLE registry_logins (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        host TEXT NOT NULL,
        username TEXT NOT NULL,
        secret TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (node_id, host)
    );
    """,
)

MAX_LOGINS = 20
STATES = ("current", "update", "unknown", "error")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def list_for_node(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Return the latest result of each image, updates first."""
    rows = conn.execute(
        "SELECT ref, state, in_use, remote_digest, error, checked_at FROM image_updates"
        " WHERE node_id = ? ORDER BY CASE state WHEN 'update' THEN 0 WHEN 'error' THEN 1"
        " ELSE 2 END, ref",
        (node_id,),
    )
    return [{**dict(r), "in_use": bool(r["in_use"])} for r in rows]


def previous_notified(conn: sqlite3.Connection, node_id: str, ref: str) -> str | None:
    """Return the digest the person was last told about for this image, if any."""
    row = conn.execute(
        "SELECT notified_digest FROM image_updates WHERE node_id = ? AND ref = ?", (node_id, ref)
    ).fetchone()
    return row["notified_digest"] if row else None


def save(
    conn: sqlite3.Connection,
    node_id: str,
    ref: str,
    *,
    state: str,
    in_use: bool,
    local: list[str],
    remote: str | None,
    error: str | None,
    notified: str | None,
) -> None:
    """Store one comparison (the caller commits)."""
    conn.execute(
        "INSERT INTO image_updates (node_id, ref, state, in_use, local_digests, remote_digest,"
        " error, checked_at, notified_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(node_id, ref) DO UPDATE SET state = excluded.state,"
        " in_use = excluded.in_use, local_digests = excluded.local_digests,"
        " remote_digest = excluded.remote_digest, error = excluded.error,"
        " checked_at = excluded.checked_at, notified_digest = excluded.notified_digest",
        (node_id, ref, state, int(in_use), json.dumps(local), remote, error, _now(), notified),
    )


def forget_others(conn: sqlite3.Connection, node_id: str, present: set[str]) -> None:
    """Drop the images that are no longer on the machine (the caller commits)."""
    for row in conn.execute(
        "SELECT ref FROM image_updates WHERE node_id = ?", (node_id,)
    ).fetchall():
        if row["ref"] not in present:
            conn.execute(
                "DELETE FROM image_updates WHERE node_id = ? AND ref = ?", (node_id, row["ref"])
            )


def list_logins(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Return the registries a machine has a login for (host and user, never the secret)."""
    rows = conn.execute(
        "SELECT host, username, created_at FROM registry_logins WHERE node_id = ? ORDER BY host",
        (node_id,),
    )
    return [dict(r) for r in rows]


def sealed_logins(conn: sqlite3.Connection, node_id: str) -> dict[str, tuple[str, str]]:
    """Return `{host: (username, encrypted secret)}` for a machine."""
    rows = conn.execute(
        "SELECT host, username, secret FROM registry_logins WHERE node_id = ?", (node_id,)
    )
    return {r["host"]: (r["username"], r["secret"]) for r in rows}


def save_login(
    conn: sqlite3.Connection, node_id: str, host: str, username: str, sealed: str, by: str
) -> bool:
    """Store or replace a login; False if the machine already holds `MAX_LOGINS` other hosts."""
    known = {
        r["host"]
        for r in conn.execute("SELECT host FROM registry_logins WHERE node_id = ?", (node_id,))
    }
    if host not in known and len(known) >= MAX_LOGINS:
        return False
    conn.execute(
        "INSERT INTO registry_logins (node_id, host, username, secret, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(node_id, host) DO UPDATE SET"
        " username = excluded.username, secret = excluded.secret,"
        " created_by = excluded.created_by, created_at = excluded.created_at",
        (node_id, host, username, sealed, by, _now()),
    )
    conn.commit()
    return True


def delete_login(conn: sqlite3.Connection, node_id: str, host: str) -> bool:
    """Forget a login; False if there was none."""
    done = conn.execute(
        "DELETE FROM registry_logins WHERE node_id = ? AND host = ?", (node_id, host)
    ).rowcount
    conn.commit()
    return bool(done)
