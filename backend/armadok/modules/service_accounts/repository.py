"""Storage of service accounts (the account itself is a row of `users` with kind `service`)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration
from armadok.core.grants import Grant, load_grants

MIGRATION = Migration(
    "service-accounts-0001-create",
    """
    CREATE TABLE service_accounts (
        user_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        follow_owner INTEGER NOT NULL DEFAULT 1,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX service_accounts_owner ON service_accounts (owner_id);
    """,
)


def add(
    conn: sqlite3.Connection, user_id: str, owner_id: str, follow_owner: bool, description: str
) -> None:
    """Record that an account is a service account run by `owner_id`."""
    conn.execute(
        "INSERT INTO service_accounts (user_id, owner_id, follow_owner, description, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, owner_id, int(follow_owner), description, datetime.now(UTC).isoformat()),
    )


def get(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    """Return a service account's row, or None if the account is not one."""
    return conn.execute("SELECT * FROM service_accounts WHERE user_id = ?", (user_id,)).fetchone()


def list_for(conn: sqlite3.Connection, owner_id: str) -> list[dict[str, Any]]:
    """Return the service accounts a person is responsible for."""
    rows = conn.execute(
        "SELECT s.user_id AS id, u.username, u.is_active, s.follow_owner, s.description,"
        " s.created_at FROM service_accounts s JOIN users u ON u.id = s.user_id"
        " WHERE s.owner_id = ? ORDER BY u.username",
        (owner_id,),
    )
    return [
        {**dict(r), "follow_owner": bool(r["follow_owner"]), "is_active": bool(r["is_active"])}
        for r in rows
    ]


def borrowed_grants(conn: sqlite3.Connection, account_id: str) -> tuple[Grant, ...]:
    """Grants a service account takes from its responsible person, while they are active.

    Only for accounts that follow their owner; the owner's own rights at this moment, so a
    right the person loses is lost by the account too.
    """
    row = get(conn, account_id)
    if row is None or not row["follow_owner"]:
        return ()
    owner = conn.execute("SELECT is_active FROM users WHERE id = ?", (row["owner_id"],)).fetchone()
    if owner is None or not owner["is_active"]:
        return ()
    return load_grants(conn, row["owner_id"])


def owner_is_inactive(conn: sqlite3.Connection, account_id: str) -> bool:
    """Whether the person responsible for a service account is disabled (or gone)."""
    row = get(conn, account_id)
    if row is None:
        return False
    owner = conn.execute("SELECT is_active FROM users WHERE id = ?", (row["owner_id"],)).fetchone()
    return owner is None or not owner["is_active"]
