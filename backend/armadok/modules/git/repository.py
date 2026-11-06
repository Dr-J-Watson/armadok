"""Storage of Git logins (per node, encrypted) and of each project's Git source."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "git-0001-create",
    """
    CREATE TABLE git_logins (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        host TEXT NOT NULL,
        username TEXT NOT NULL,
        secret TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (node_id, host)
    );
    CREATE TABLE project_sources (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        project TEXT NOT NULL,
        url TEXT NOT NULL,
        branch TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (node_id, project)
    );
    """,
)

MAX_LOGINS = 20


def _now() -> str:
    return datetime.now(UTC).isoformat()


def list_logins(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Return the hosts a node has a login for (host and user, never the token)."""
    rows = conn.execute(
        "SELECT host, username, created_at FROM git_logins WHERE node_id = ? ORDER BY host",
        (node_id,),
    )
    return [dict(r) for r in rows]


def sealed_login(conn: sqlite3.Connection, node_id: str, host: str) -> tuple[str, str] | None:
    """Return `(username, encrypted token)` for a host, or None."""
    row = conn.execute(
        "SELECT username, secret FROM git_logins WHERE node_id = ? AND host = ?", (node_id, host)
    ).fetchone()
    return (row["username"], row["secret"]) if row else None


def save_login(
    conn: sqlite3.Connection, node_id: str, host: str, username: str, sealed: str, by: str
) -> bool:
    """Store or replace a login; False if the node already holds `MAX_LOGINS` other hosts."""
    known = {
        r["host"] for r in conn.execute("SELECT host FROM git_logins WHERE node_id = ?", (node_id,))
    }
    if host not in known and len(known) >= MAX_LOGINS:
        return False
    conn.execute(
        "INSERT INTO git_logins (node_id, host, username, secret, created_by, created_at)"
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
        "DELETE FROM git_logins WHERE node_id = ? AND host = ?", (node_id, host)
    ).rowcount
    conn.commit()
    return bool(done)


def get_source(conn: sqlite3.Connection, node_id: str, project: str) -> dict[str, Any] | None:
    """Return a project's Git source (address and branch), or None."""
    row = conn.execute(
        "SELECT url, branch, created_at FROM project_sources WHERE node_id = ? AND project = ?",
        (node_id, project),
    ).fetchone()
    return dict(row) if row else None


def set_source(
    conn: sqlite3.Connection, node_id: str, project: str, url: str, branch: str, by: str
) -> None:
    """Remember where a project came from (the caller commits)."""
    conn.execute(
        "INSERT INTO project_sources (node_id, project, url, branch, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(node_id, project) DO UPDATE SET"
        " url = excluded.url, branch = excluded.branch",
        (node_id, project, url, branch, by, _now()),
    )


def delete_source(conn: sqlite3.Connection, node_id: str, project: str) -> bool:
    """Forget a project's Git source; False if it had none."""
    done = conn.execute(
        "DELETE FROM project_sources WHERE node_id = ? AND project = ?", (node_id, project)
    ).rowcount
    conn.commit()
    return bool(done)
