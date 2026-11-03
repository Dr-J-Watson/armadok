"""Database access for DNS accounts and domains."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "dns-0001-create",
    """
    -- `credentials` is encrypted (keys and tokens): never returned, never logged.
    -- An account belongs to a person, not to a node.
    CREATE TABLE dns_accounts (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        name TEXT NOT NULL,
        credentials TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX dns_accounts_owner ON dns_accounts (owner_id);
    -- A domain may have no account (local DNS) and may be attached to one node, whose members
    -- then see it without ever seeing the account's keys.
    CREATE TABLE dns_domains (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        account_id TEXT,
        name TEXT NOT NULL,
        node_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (owner_id, name)
    );
    CREATE INDEX dns_domains_node ON dns_domains (node_id);
    """,
)

RESTRICTIONS_MIGRATION = Migration(
    "dns-0002-member-restrictions",
    """
    -- What the members of the node a domain is attached to may write with the owner's account.
    ALTER TABLE dns_domains ADD COLUMN member_types TEXT NOT NULL DEFAULT 'A,AAAA,CNAME';
    ALTER TABLE dns_domains ADD COLUMN member_apex INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE dns_domains ADD COLUMN member_names TEXT NOT NULL DEFAULT '';
    """,
)

MAX_ACCOUNTS = 20
MAX_DOMAINS = 500


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- accounts -----------------------------------------------------------------------------


def create_account(
    conn: sqlite3.Connection, owner_id: str, provider: str, name: str, sealed: str
) -> str:
    """Store an account (credentials already encrypted) and return its id."""
    account_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO dns_accounts (id, owner_id, provider, name, credentials, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, owner_id, provider, name, sealed, _now()),
    )
    conn.commit()
    return account_id


def list_accounts(conn: sqlite3.Connection, owner_id: str) -> list[dict[str, Any]]:
    """Return the owner's accounts WITHOUT credentials."""
    rows = conn.execute(
        "SELECT id, provider, name, created_at FROM dns_accounts WHERE owner_id = ?"
        " ORDER BY created_at",
        (owner_id,),
    )
    return [dict(r) for r in rows]


def get_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    """Return an account with its sealed credentials (callers check ownership)."""
    return conn.execute("SELECT * FROM dns_accounts WHERE id = ?", (account_id,)).fetchone()


def delete_account(conn: sqlite3.Connection, owner_id: str, account_id: str) -> bool:
    """Delete one of the owner's accounts."""
    cur = conn.execute(
        "DELETE FROM dns_accounts WHERE id = ? AND owner_id = ?", (account_id, owner_id)
    )
    conn.commit()
    return cur.rowcount == 1


def domains_using(conn: sqlite3.Connection, account_id: str) -> int:
    """Count the domains that use an account."""
    return conn.execute(
        "SELECT COUNT(*) FROM dns_domains WHERE account_id = ?", (account_id,)
    ).fetchone()[0]


# --- domains ------------------------------------------------------------------------------


def create_domain(
    conn: sqlite3.Connection, owner_id: str, account_id: str | None, name: str
) -> str | None:
    """Add a domain; return its id, or None if the owner already has it."""
    domain_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO dns_domains (id, owner_id, account_id, name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (domain_id, owner_id, account_id, name, _now()),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    return domain_id


def get_domain(conn: sqlite3.Connection, domain_id: str) -> sqlite3.Row | None:
    """Return a domain row (callers check access)."""
    return conn.execute("SELECT * FROM dns_domains WHERE id = ?", (domain_id,)).fetchone()


def list_domains(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every domain (callers filter by access)."""
    return conn.execute("SELECT * FROM dns_domains ORDER BY name").fetchall()


def attach(
    conn: sqlite3.Connection,
    domain_id: str,
    node_id: str | None,
    member_types: str = "A,AAAA,CNAME",
    member_apex: bool = False,
    member_names: str = "",
) -> None:
    """Attach a domain to a node (with what its members may write), or detach it."""
    conn.execute(
        "UPDATE dns_domains SET node_id = ?, member_types = ?, member_apex = ?, member_names = ?"
        " WHERE id = ?",
        (node_id, member_types, int(member_apex), member_names, domain_id),
    )
    conn.commit()


def detach_node(conn: sqlite3.Connection, node_id: str) -> None:
    """Detach every domain from a node that no longer exists."""
    conn.execute("UPDATE dns_domains SET node_id = NULL WHERE node_id = ?", (node_id,))
    conn.commit()


def count_domains(conn: sqlite3.Connection, owner_id: str) -> int:
    """Count the domains of one owner (the quota is per person, not global)."""
    return conn.execute(
        "SELECT COUNT(*) FROM dns_domains WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]


def delete_domain(conn: sqlite3.Connection, domain_id: str) -> None:
    """Delete a domain (the zone itself is untouched at the provider)."""
    conn.execute("DELETE FROM dns_domains WHERE id = ?", (domain_id,))
    conn.commit()
