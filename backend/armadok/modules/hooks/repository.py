"""Storage of incoming hooks. Only a hash of each token is kept."""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration
from armadok.modules.users.security import token_hash

MIGRATION = Migration(
    "hooks-0001-hooks",
    """
    CREATE TABLE hooks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        last_result TEXT
    );
    CREATE INDEX hooks_creator ON hooks (created_by);
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create(
    conn: sqlite3.Connection,
    name: str,
    action: str,
    target: dict[str, Any],
    who: str,
    expires_at: datetime | None,
) -> tuple[str, str]:
    """Store a hook; return `(id, token)`. The token is not stored and cannot be shown again."""
    hook_id = uuid.uuid4().hex[:16]
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO hooks (id, name, action, target, token_hash, created_by, created_at,"
        " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hook_id,
            name,
            action,
            json.dumps(target, sort_keys=True),
            token_hash(token),
            who,
            _now(),
            expires_at.isoformat() if expires_at else None,
        ),
    )
    conn.commit()
    return hook_id, token


def view(row: sqlite3.Row) -> dict[str, Any]:
    """Return the public face of a hook (never the token hash)."""
    return {
        "id": row["id"],
        "name": row["name"],
        "action": row["action"],
        "target": json.loads(row["target"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
        "last_used_at": row["last_used_at"],
        "last_result": row["last_result"],
    }


def get(conn: sqlite3.Connection, hook_id: str) -> sqlite3.Row | None:
    """Return one hook row, or None."""
    return conn.execute("SELECT * FROM hooks WHERE id = ?", (hook_id,)).fetchone()


def list_for(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Return a person's own hooks, newest first."""
    rows = conn.execute(
        "SELECT * FROM hooks WHERE created_by = ? ORDER BY created_at DESC", (user_id,)
    )
    return [view(r) for r in rows]


def revoke(conn: sqlite3.Connection, hook_id: str, user_id: str) -> bool:
    """Revoke one of the person's hooks; return False if it is not theirs or does not exist."""
    cur = conn.execute(
        "UPDATE hooks SET revoked = 1 WHERE id = ? AND created_by = ?", (hook_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


def claim_call(conn: sqlite3.Connection, hook_id: str, min_interval_seconds: int) -> bool:
    """Record a call unless the last one was too recent; True means this call may go ahead."""
    now = datetime.now(UTC)
    cutoff = now.timestamp() - min_interval_seconds
    row = get(conn, hook_id)
    if row is None:
        return False
    last = row["last_used_at"]
    if last and datetime.fromisoformat(last).timestamp() > cutoff:
        return False
    # Compare-and-set, so two simultaneous calls cannot both pass.
    cur = conn.execute(
        "UPDATE hooks SET last_used_at = ? WHERE id = ? AND last_used_at IS ?",
        (now.isoformat(), hook_id, last),
    )
    conn.commit()
    return cur.rowcount == 1


def set_result(conn: sqlite3.Connection, hook_id: str, result: str) -> None:
    """Keep the outcome of the last call (a status word or an error code, never content)."""
    conn.execute("UPDATE hooks SET last_result = ? WHERE id = ?", (result, hook_id))
    conn.commit()


def revoke_for_object(conn: sqlite3.Connection, change: Any) -> None:
    """Revoke the former owner's hooks on an object that changed hands.

    Their token is in the former owner's tools, so unlike a schedule it cannot just be paused:
    the new owner makes a new hook. Part of the owner change's transaction.
    """
    conn.execute(
        "UPDATE hooks SET revoked = 1, last_result = 'ownership.changed'"
        " WHERE created_by = ? AND revoked = 0 AND json_extract(target, '$.node_id') = ?"
        " AND json_extract(target, '$.' || ?) = ?",
        (change.old_owner, change.node_id, change.object_type, change.object_id),
    )
