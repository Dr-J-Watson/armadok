"""Storage of emergency sessions. They are notices as much as sessions: they cannot be erased."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.db import Migration
from armadok.core.grants import Grant, load_grants, parse_expiry

SESSION_MINUTES = 30

MIGRATION = Migration(
    "emergency-0001-sessions",
    """
    CREATE TABLE emergency_sessions (
        id TEXT PRIMARY KEY,
        admin_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        started_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        ended_at TEXT,
        ended_by TEXT,
        acknowledged_at TEXT,
        acknowledged_by TEXT
    );
    CREATE INDEX emergency_node ON emergency_sessions (node_id, started_at);
    CREATE INDEX emergency_admin ON emergency_sessions (admin_id, expires_at);
    -- The owner may acknowledge a notice but never erase it, and nobody rewrites what it says.
    CREATE TRIGGER emergency_no_delete BEFORE DELETE ON emergency_sessions
    BEGIN SELECT RAISE(ABORT, 'emergency sessions cannot be erased'); END;
    CREATE TRIGGER emergency_frozen BEFORE UPDATE ON emergency_sessions
    WHEN NEW.id != OLD.id OR NEW.admin_id != OLD.admin_id OR NEW.node_id != OLD.node_id
      OR NEW.reason != OLD.reason OR NEW.started_at != OLD.started_at
      OR NEW.expires_at != OLD.expires_at
      OR (OLD.ended_at IS NOT NULL AND (NEW.ended_at IS NOT OLD.ended_at
                                         OR NEW.ended_by IS NOT OLD.ended_by))
      OR (OLD.acknowledged_at IS NOT NULL AND NEW.acknowledged_at IS NOT OLD.acknowledged_at)
    BEGIN SELECT RAISE(ABORT, 'emergency sessions are not rewritten'); END;
    """,
)


def _now() -> datetime:
    return datetime.now(UTC)


def start(conn: sqlite3.Connection, admin_id: str, node_id: str, reason: str) -> dict[str, Any]:
    """Open a 30-minute session and return it."""
    sid = uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO emergency_sessions (id, admin_id, node_id, reason, started_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            sid,
            admin_id,
            node_id,
            reason,
            now.isoformat(),
            (now + timedelta(minutes=SESSION_MINUTES)).isoformat(),
        ),
    )
    conn.commit()
    return get(conn, sid)  # type: ignore[return-value]


def get(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """Return one session, or None."""
    row = conn.execute("SELECT * FROM emergency_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def active_for(conn: sqlite3.Connection, admin_id: str) -> list[dict[str, Any]]:
    """Return the sessions of this administrator that are open and not past their end."""
    rows = conn.execute(
        "SELECT * FROM emergency_sessions"
        " WHERE admin_id = ? AND ended_at IS NULL AND expires_at > ?",
        (admin_id, _now().isoformat()),
    )
    return [dict(r) for r in rows]


def end(conn: sqlite3.Connection, session_id: str, by: str) -> None:
    """End a session now (the end and who asked are kept; the session never disappears)."""
    conn.execute(
        "UPDATE emergency_sessions SET ended_at = ?, ended_by = ?"
        " WHERE id = ? AND ended_at IS NULL",
        (_now().isoformat(), by, session_id),
    )
    conn.commit()


COOLDOWN_MINUTES = 10


def cooling_down(conn: sqlite3.Connection, admin_id: str, node_id: str) -> bool:
    """Whether the owner cut this administrator's last session on the node a moment ago."""
    since = (_now() - timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM emergency_sessions WHERE admin_id = ? AND node_id = ?"
        " AND ended_by IS NOT NULL AND ended_by != admin_id AND ended_at > ?",
        (admin_id, node_id, since),
    ).fetchone()
    return row is not None


def may_still_use(conn: sqlite3.Connection, account_id: str) -> bool:
    """Whether the account is active and still holds the emergency right everywhere.

    Checked at every use, so withdrawing the right (or disabling the account) cuts open
    sessions short instead of leaving them to run out.
    """
    user = conn.execute("SELECT is_active FROM users WHERE id = ?", (account_id,)).fetchone()
    if user is None or not user["is_active"]:
        return False
    return any(
        g.right == "emergency.use"
        and g.scope == "*"
        and (g.expires_at is None or g.expires_at > _now())
        for g in load_grants(conn, account_id)
    )


def for_node(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Return every session on a node, newest first, with the administrator's name."""
    rows = conn.execute(
        "SELECT s.*, u.username AS admin_name FROM emergency_sessions s"
        " LEFT JOIN users u ON u.id = s.admin_id WHERE s.node_id = ? ORDER BY s.started_at DESC",
        (node_id,),
    )
    return [dict(r) for r in rows]


def acknowledge(conn: sqlite3.Connection, session_id: str, user_id: str) -> None:
    """Record that the owner saw it (the first acknowledgement is kept)."""
    conn.execute(
        "UPDATE emergency_sessions SET acknowledged_at = ?, acknowledged_by = ?"
        " WHERE id = ? AND acknowledged_at IS NULL",
        (_now().isoformat(), user_id, session_id),
    )
    conn.commit()


# --- what an open session lends -------------------------------------------------------------


def session_grants(conn: sqlite3.Connection, account_id: str) -> tuple[Grant, ...]:
    """Return the administrator-role rights on each node the account has an open session on.

    Each ends when the session does. Read from the built-in role as stored, so a right added
    to the role later is included without touching this module.
    """
    sessions = active_for(conn, account_id)
    if not sessions or not may_still_use(conn, account_id):
        return ()
    rights = [
        r["right"]
        for r in conn.execute(
            "SELECT right FROM role_rights WHERE role_id = 'builtin-administrator'"
        )
    ]
    return tuple(
        Grant(right, f"node:{s['node_id']}", parse_expiry(s["expires_at"]))
        for s in sessions
        for right in rights
    )


def session_scopes(conn: sqlite3.Connection, account_id: str) -> frozenset[str]:
    """Return the suspended-node scopes this account may use all the same, for now."""
    if not may_still_use(conn, account_id):
        return frozenset()
    return frozenset(f"node:{s['node_id']}" for s in active_for(conn, account_id))
