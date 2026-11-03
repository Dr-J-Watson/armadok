"""Database access for API keys. Only a hash of each key's secret is stored."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime

from armadok.core.db import Migration
from armadok.modules.users.security import token_hash

KEY_PREFIX = "arm_"

MIGRATION = Migration(
    "apikeys-0001-create",
    """
    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES users (id),
        name TEXT NOT NULL,
        secret_hash TEXT NOT NULL,
        rights TEXT NOT NULL,
        read_only INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT,
        last_used_at TEXT,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX api_keys_owner ON api_keys (owner_id);
    """,
)

_PUBLIC = "id, owner_id, name, rights, read_only, expires_at, last_used_at, revoked, created_at"


def _public(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["rights"] = json.loads(data["rights"])
    data["read_only"] = bool(data["read_only"])
    data["revoked"] = bool(data["revoked"])
    return data


def create_key(
    conn: sqlite3.Connection,
    owner_id: str,
    name: str,
    rights: list[str],
    read_only: bool,
    expires_at: datetime | None,
) -> tuple[str, dict]:
    """Insert a key; return `(full_key, public_record)`.

    The full key (`arm_<id>_<secret>`) exists only in this return value: the database keeps
    the id and a hash of the secret, so it cannot be shown again.
    """
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_keys (id, owner_id, name, secret_hash, rights, read_only, expires_at,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key_id,
            owner_id,
            name,
            token_hash(secret),
            json.dumps(sorted(rights)),
            int(read_only),
            expires_at.isoformat() if expires_at else None,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    return f"{KEY_PREFIX}{key_id}_{secret}", get_key(conn, key_id)  # type: ignore[return-value]


def get_key(conn: sqlite3.Connection, key_id: str) -> dict | None:
    """Return a key's public record, or None."""
    row = conn.execute(f"SELECT {_PUBLIC} FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _public(row) if row else None


def list_keys(conn: sqlite3.Connection, owner_id: str) -> list[dict]:
    """Return the keys of one owner, newest first."""
    rows = conn.execute(
        f"SELECT {_PUBLIC} FROM api_keys WHERE owner_id = ? ORDER BY created_at DESC, id",
        (owner_id,),
    )
    return [_public(r) for r in rows]


def get_key_for_auth(conn: sqlite3.Connection, key_id: str) -> sqlite3.Row | None:
    """Return the full row (with secret hash) of a key joined with its owner's state."""
    return conn.execute(
        "SELECT k.*, u.is_active AS owner_active FROM api_keys k"
        " JOIN users u ON u.id = k.owner_id WHERE k.id = ?",
        (key_id,),
    ).fetchone()


def touch(conn: sqlite3.Connection, key_id: str) -> None:
    """Record that the key was just used."""
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), key_id)
    )
    conn.commit()


def revoke(conn: sqlite3.Connection, owner_id: str, key_id: str) -> bool:
    """Revoke one of the owner's keys at once; False if it does not exist."""
    cur = conn.execute(
        "UPDATE api_keys SET revoked = 1 WHERE id = ? AND owner_id = ?", (key_id, owner_id)
    )
    conn.commit()
    return cur.rowcount == 1
