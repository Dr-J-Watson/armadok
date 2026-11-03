"""Database access for the inbox, the channels and the rules of each account."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "notifications-0001-create",
    """
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        event TEXT NOT NULL,
        severity TEXT NOT NULL,
        node_id TEXT,
        params TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX notifications_user ON notifications (user_id, read, id);
    -- `config` is encrypted: it holds URLs, tokens and signing secrets, never returned.
    CREATE TABLE notification_channels (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        config TEXT NOT NULL,
        last_status TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX notification_channels_owner ON notification_channels (owner_id);
    -- `target` is "in_app" or a channel id.
    CREATE TABLE notification_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        event TEXT NOT NULL,
        target TEXT NOT NULL,
        min_severity TEXT NOT NULL DEFAULT 'info',
        node_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX notification_rules_user ON notification_rules (user_id, event);
    """,
)

INDEX_MIGRATION = Migration(
    "notifications-0002-inbox-index",
    "CREATE INDEX notifications_user_id ON notifications (user_id, id);",
)

SEVERITIES = ("info", "warning", "critical")
MAX_INBOX = 1000  # per account: older entries are dropped as new ones arrive
MAX_CHANNELS = 20
MAX_RULES = 500


def severity_rank(value: str) -> int:
    """Return how serious a severity is (higher is more serious)."""
    return SEVERITIES.index(value) if value in SEVERITIES else 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- inbox --------------------------------------------------------------------------------


def add_to_inbox(
    conn: sqlite3.Connection,
    user_id: str,
    event: str,
    severity: str,
    node_id: str | None,
    params: dict[str, Any],
) -> None:
    """Add an entry to an account's inbox, keeping only the most recent `MAX_INBOX`."""
    conn.execute(
        "INSERT INTO notifications (user_id, event, severity, node_id, params, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, event, severity, node_id, json.dumps(params), _now()),
    )
    conn.execute(
        "DELETE FROM notifications WHERE user_id = ? AND id NOT IN"
        " (SELECT id FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, MAX_INBOX),
    )
    conn.commit()


def list_inbox(
    conn: sqlite3.Connection, user_id: str, unread_only: bool, before_id: int | None, limit: int
) -> list[dict[str, Any]]:
    """Return an account's entries, newest first."""
    where, args = ["user_id = ?"], [user_id]
    if unread_only:
        where.append("read = 0")
    if before_id:
        where.append("id < ?")
        args.append(before_id)
    rows = conn.execute(
        f"SELECT * FROM notifications WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        (*args, limit),
    )
    return [{**dict(r), "params": json.loads(r["params"]), "read": bool(r["read"])} for r in rows]


def unread_count(conn: sqlite3.Connection, user_id: str) -> int:
    """Count an account's unread entries."""
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0", (user_id,)
    ).fetchone()[0]


def mark_read(conn: sqlite3.Connection, user_id: str, ids: list[int] | None) -> None:
    """Mark some (or all) of an account's entries as read."""
    if ids is None:
        conn.execute("UPDATE notifications SET read = 1 WHERE user_id = ?", (user_id,))
    elif ids:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE notifications SET read = 1 WHERE user_id = ? AND id IN ({marks})",
            (user_id, *ids),
        )
    conn.commit()


# --- channels -----------------------------------------------------------------------------


def create_channel(
    conn: sqlite3.Connection, owner_id: str, kind: str, name: str, sealed_config: str
) -> str:
    """Store a channel (its config already encrypted) and return its id."""
    channel_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO notification_channels (id, owner_id, kind, name, config, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (channel_id, owner_id, kind, name, sealed_config, _now()),
    )
    conn.commit()
    return channel_id


def list_channels(conn: sqlite3.Connection, owner_id: str) -> list[dict[str, Any]]:
    """Return an account's channels WITHOUT their configuration (it holds secrets)."""
    rows = conn.execute(
        "SELECT id, kind, name, last_status, created_at FROM notification_channels"
        " WHERE owner_id = ? ORDER BY created_at",
        (owner_id,),
    )
    return [dict(r) for r in rows]


def get_channel(conn: sqlite3.Connection, owner_id: str, channel_id: str) -> sqlite3.Row | None:
    """Return one of the account's channels with its sealed configuration, or None."""
    return conn.execute(
        "SELECT * FROM notification_channels WHERE id = ? AND owner_id = ?", (channel_id, owner_id)
    ).fetchone()


def delete_channel(conn: sqlite3.Connection, owner_id: str, channel_id: str) -> bool:
    """Delete a channel and the rules that pointed at it."""
    cur = conn.execute(
        "DELETE FROM notification_channels WHERE id = ? AND owner_id = ?", (channel_id, owner_id)
    )
    conn.execute(
        "DELETE FROM notification_rules WHERE user_id = ? AND target = ?", (owner_id, channel_id)
    )
    conn.commit()
    return cur.rowcount == 1


def set_status(conn: sqlite3.Connection, channel_id: str, status: str) -> None:
    """Remember how the last delivery went (`ok` or an error code)."""
    conn.execute(
        "UPDATE notification_channels SET last_status = ? WHERE id = ?", (status, channel_id)
    )
    conn.commit()


# --- rules --------------------------------------------------------------------------------


def list_rules(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Return an account's rules."""
    rows = conn.execute(
        "SELECT event, target, min_severity, node_id, enabled FROM notification_rules"
        " WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    return [{**dict(r), "enabled": bool(r["enabled"])} for r in rows]


def replace_rules(conn: sqlite3.Connection, user_id: str, rules: list[dict[str, Any]]) -> None:
    """Replace all of an account's rules in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM notification_rules WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO notification_rules"
            " (user_id, event, target, min_severity, node_id, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    user_id,
                    r["event"],
                    r["target"],
                    r["min_severity"],
                    r["node_id"],
                    int(r["enabled"]),
                )
                for r in rules
            ],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
