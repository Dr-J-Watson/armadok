"""Storage of rights granted to principals (people, service accounts).

The table lives in the core because several modules read or write grants
(users, nodes, API keys). Evaluation stays in `rights.py`.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from armadok.core.db import Migration
from armadok.core.rights import Grant

GRANTS_MIGRATION = Migration(
    "core-0002-grants",
    """
    CREATE TABLE grants (
        id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL,
        right TEXT NOT NULL,
        scope TEXT NOT NULL,
        expires_at TEXT,
        granted_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX grants_principal ON grants (principal_id);
    """,
)


def add_grant(
    conn: sqlite3.Connection,
    principal_id: str,
    grant: Grant,
    granted_by: str,
    *,
    commit: bool = True,
) -> str:
    """Store a grant and return its id."""
    grant_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO grants (id, principal_id, right, scope, expires_at, granted_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            grant_id,
            principal_id,
            grant.right,
            grant.scope,
            grant.expires_at.isoformat() if grant.expires_at else None,
            granted_by,
            datetime.now(UTC).isoformat(),
        ),
    )
    if commit:
        conn.commit()
    return grant_id


def list_grants(conn: sqlite3.Connection, principal_id: str) -> list[dict]:
    """Return the stored grants of a principal, with their ids."""
    rows = conn.execute(
        "SELECT id, right, scope, expires_at, granted_by, created_at FROM grants"
        " WHERE principal_id = ? ORDER BY created_at, id",
        (principal_id,),
    )
    return [dict(r) for r in rows]


def load_grants(conn: sqlite3.Connection, principal_id: str) -> tuple[Grant, ...]:
    """Return a principal's grants as evaluation objects."""
    return tuple(
        Grant(r["right"], r["scope"], parse_expiry(r["expires_at"]))
        for r in list_grants(conn, principal_id)
    )


def parse_expiry(value: str | None) -> datetime | None:
    """Read a stored expiry; a naive value is taken as UTC so comparisons never raise."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def remove_grant(conn: sqlite3.Connection, principal_id: str, grant_id: str) -> bool:
    """Delete one grant of a principal; return False if it did not exist."""
    cur = conn.execute(
        "DELETE FROM grants WHERE id = ? AND principal_id = ?", (grant_id, principal_id)
    )
    conn.commit()
    return cur.rowcount > 0
