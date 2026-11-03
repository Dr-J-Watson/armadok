"""Database access for workers and enrolment tokens."""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from armadok.core.db import Migration

ENROL_TTL = timedelta(minutes=15)

MIGRATION = Migration(
    "workers-0001-create",
    """
    CREATE TABLE workers (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        public_key TEXT NOT NULL,
        enrolled_at TEXT NOT NULL,
        last_seen_at TEXT,
        revoked INTEGER NOT NULL DEFAULT 0
    );
    -- One active worker per machine (node); a revoked one no longer counts.
    CREATE UNIQUE INDEX workers_active_node ON workers (node_id) WHERE revoked = 0;
    -- `key` is the PUBLIC half of a key derived from the token secret: enough to verify an
    -- enrolment proof, useless to make one. The secret itself is never stored.
    CREATE TABLE enrolment_tokens (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        key TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );
    """,
)


def _now() -> datetime:
    return datetime.now(UTC)


def create_enrolment(
    conn: sqlite3.Connection, node_id: str, key_hex_of: Callable[[str], str]
) -> tuple[str, str]:
    """Create a one-time token for a node; return `(token, expires_at)`.

    Earlier unused tokens of that node stop working. `key_hex_of(secret)` derives the stored
    public verification key, so this module never keeps the secret.
    """
    token_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(24)
    expires = (_now() + ENROL_TTL).isoformat()
    conn.execute("UPDATE enrolment_tokens SET used = 1 WHERE node_id = ? AND used = 0", (node_id,))
    conn.execute(
        "INSERT INTO enrolment_tokens (id, node_id, key, expires_at) VALUES (?, ?, ?, ?)",
        (token_id, node_id, key_hex_of(secret), expires),
    )
    conn.commit()
    return f"armtok_{token_id}_{secret}", expires


def get_open_enrolment(conn: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    """Return an unused, unexpired token row, or None."""
    row = conn.execute(
        "SELECT * FROM enrolment_tokens WHERE id = ? AND used = 0", (token_id,)
    ).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    return row


def enrol_worker(
    conn: sqlite3.Connection, token_id: str, node_id: str, public_key_hex: str
) -> str | None:
    """Spend the token and register the worker, all or nothing; return its id.

    The token is claimed with one atomic statement, so two workers presenting it at the same
    moment cannot both enrol. A previous worker of the node is revoked (machine replacement).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        claimed = conn.execute(
            "UPDATE enrolment_tokens SET used = 1 WHERE id = ? AND used = 0 AND expires_at > ?",
            (token_id, _now().isoformat()),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            return None
        conn.execute("UPDATE workers SET revoked = 1 WHERE node_id = ?", (node_id,))
        worker_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO workers (id, node_id, public_key, enrolled_at) VALUES (?, ?, ?, ?)",
            (worker_id, node_id, public_key_hex, _now().isoformat()),
        )
        conn.commit()
        return worker_id
    except BaseException:
        conn.rollback()
        raise


def get_active_worker(conn: sqlite3.Connection, worker_id: str) -> sqlite3.Row | None:
    """Return a non-revoked worker by id, or None."""
    return conn.execute(
        "SELECT * FROM workers WHERE id = ? AND revoked = 0", (worker_id,)
    ).fetchone()


def get_worker_of_node(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    """Return the active worker of a node, or None."""
    return conn.execute(
        "SELECT * FROM workers WHERE node_id = ? AND revoked = 0", (node_id,)
    ).fetchone()


def touch(conn: sqlite3.Connection, worker_id: str) -> None:
    """Record that the worker was seen now."""
    conn.execute(
        "UPDATE workers SET last_seen_at = ? WHERE id = ?", (_now().isoformat(), worker_id)
    )
    conn.commit()


def revoke_node_worker(conn: sqlite3.Connection, node_id: str) -> bool:
    """Revoke the node's active worker and its pending tokens; False if it had none."""
    conn.execute("UPDATE enrolment_tokens SET used = 1 WHERE node_id = ? AND used = 0", (node_id,))
    cur = conn.execute(
        "UPDATE workers SET revoked = 1 WHERE node_id = ? AND revoked = 0", (node_id,)
    )
    conn.commit()
    return cur.rowcount > 0
