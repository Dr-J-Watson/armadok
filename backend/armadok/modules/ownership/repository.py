"""Storage of who owns what inside a node, and of the gifts waiting to be accepted.

An object with no row belongs to the node's owner. A row says someone else owns it. Owning an
object means holding the rights its kind declares (`Ownable.rights`) on that object alone:
`grants_of` turns the rows into grants on the fly, so nothing can go stale when a row changes.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.db import Migration
from armadok.core.rights import Grant

# A gift that nobody answers is forgotten after this long.
OFFER_DAYS = 14

MIGRATION = Migration(
    "ownership-0001-owners",
    """
    CREATE TABLE object_owners (
        node_id TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        since TEXT NOT NULL,
        PRIMARY KEY (node_id, object_type, object_id)
    );
    CREATE INDEX object_owners_owner ON object_owners (owner_id);
    CREATE TABLE ownership_offers (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        from_user TEXT NOT NULL,
        to_user TEXT NOT NULL,
        with_notes INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX ownership_offers_to ON ownership_offers (to_user);
    CREATE INDEX ownership_offers_from ON ownership_offers (from_user);
    CREATE TABLE ownership_offer_items (
        offer_id TEXT NOT NULL REFERENCES ownership_offers(id) ON DELETE CASCADE,
        object_type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        PRIMARY KEY (offer_id, object_type, object_id)
    );
    """,
)


def _now() -> datetime:
    return datetime.now(UTC)


def owner_of(
    conn: sqlite3.Connection, node_id: str, object_type: str, object_id: str
) -> str | None:
    """Return the owner of an object: its row, else the node's owner (None if no such node)."""
    row = conn.execute(
        "SELECT owner_id FROM object_owners"
        " WHERE node_id = ? AND object_type = ? AND object_id = ?",
        (node_id, object_type, object_id),
    ).fetchone()
    if row is not None:
        return row["owner_id"]
    node = conn.execute("SELECT owner FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return node["owner"] if node else None


def set_owner(
    conn: sqlite3.Connection, node_id: str, object_type: str, object_id: str, owner_id: str
) -> None:
    """Make `owner_id` the owner of an object (the caller commits)."""
    conn.execute(
        "INSERT INTO object_owners (node_id, object_type, object_id, owner_id, since)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id, object_type, object_id)"
        " DO UPDATE SET owner_id = excluded.owner_id, since = excluded.since",
        (node_id, object_type, object_id, owner_id, _now().isoformat()),
    )


def clear_owner(conn: sqlite3.Connection, node_id: str, object_type: str, object_id: str) -> None:
    """Give an object back to the node's owner by forgetting its row (the caller commits)."""
    conn.execute(
        "DELETE FROM object_owners WHERE node_id = ? AND object_type = ? AND object_id = ?",
        (node_id, object_type, object_id),
    )


def owned_by(conn: sqlite3.Connection, node_id: str, owner_id: str) -> list[tuple[str, str]]:
    """Return the `(type, id)` of the objects a person owns by a row on this node."""
    rows = conn.execute(
        "SELECT object_type, object_id FROM object_owners WHERE node_id = ? AND owner_id = ?"
        " ORDER BY object_type, object_id",
        (node_id, owner_id),
    )
    return [(r["object_type"], r["object_id"]) for r in rows]


def rows_of_node(conn: sqlite3.Connection, node_id: str) -> dict[tuple[str, str], str]:
    """Return `{(type, id): owner id}` for every object of a node that has its own row."""
    rows = conn.execute(
        "SELECT object_type, object_id, owner_id FROM object_owners WHERE node_id = ?", (node_id,)
    )
    return {(r["object_type"], r["object_id"]): r["owner_id"] for r in rows}


def grants_of(
    conn: sqlite3.Connection, account_id: str, rights_by_type: dict[str, tuple[str, ...]]
) -> tuple[Grant, ...]:
    """Return the grants a person has by owning objects: their kind's rights, on that object."""
    out: list[Grant] = []
    rows = conn.execute(
        "SELECT o.node_id, o.object_type, o.object_id FROM object_owners o"
        " JOIN nodes n ON n.id = o.node_id WHERE o.owner_id = ? AND n.deleted_at IS NULL",
        (account_id,),
    )
    for row in rows:
        scope = f"node:{row['node_id']}/{row['object_type']}:{row['object_id']}"
        out.extend(Grant(right, scope) for right in rights_by_type.get(row["object_type"], ()))
    return tuple(out)


def purge_expired(conn: sqlite3.Connection) -> None:
    """Forget the gifts nobody answered in time (the caller commits)."""
    conn.execute("DELETE FROM ownership_offers WHERE expires_at <= ?", (_now().isoformat(),))


def create_offer(
    conn: sqlite3.Connection,
    node_id: str,
    from_user: str,
    to_user: str,
    items: list[tuple[str, str]],
    with_notes: bool,
) -> str:
    """Store a gift of one or more objects and return its id (the caller commits)."""
    offer_id = uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO ownership_offers (id, node_id, from_user, to_user, with_notes, created_at,"
        " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            offer_id,
            node_id,
            from_user,
            to_user,
            int(with_notes),
            now.isoformat(),
            (now + timedelta(days=OFFER_DAYS)).isoformat(),
        ),
    )
    conn.executemany(
        "INSERT INTO ownership_offer_items (offer_id, object_type, object_id) VALUES (?, ?, ?)",
        [(offer_id, t, i) for t, i in items],
    )
    return offer_id


def pending_from(conn: sqlite3.Connection, user_id: str) -> int:
    """Return how many gifts a person has sent that nobody answered yet."""
    return conn.execute(
        "SELECT COUNT(*) FROM ownership_offers WHERE from_user = ?", (user_id,)
    ).fetchone()[0]


def pending_to(conn: sqlite3.Connection, user_id: str) -> int:
    """Return how many gifts wait for a person's answer."""
    return conn.execute(
        "SELECT COUNT(*) FROM ownership_offers WHERE to_user = ?", (user_id,)
    ).fetchone()[0]


def get_offer(conn: sqlite3.Connection, offer_id: str) -> dict[str, Any] | None:
    """Return a live (not expired) gift with its items, or None."""
    row = conn.execute(
        "SELECT * FROM ownership_offers WHERE id = ? AND expires_at > ?",
        (offer_id, _now().isoformat()),
    ).fetchone()
    return _with_items(conn, row) if row else None


def offers_for(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Return the live gifts a person gave or was given, newest first."""
    rows = conn.execute(
        "SELECT o.*, n.name AS node_name, f.username AS from_name, t.username AS to_name"
        " FROM ownership_offers o JOIN nodes n ON n.id = o.node_id"
        " LEFT JOIN users f ON f.id = o.from_user LEFT JOIN users t ON t.id = o.to_user"
        " WHERE (o.from_user = ? OR o.to_user = ?) AND o.expires_at > ?"
        " ORDER BY o.created_at DESC",
        (user_id, user_id, _now().isoformat()),
    ).fetchall()
    return [_with_items(conn, r) for r in rows]


def delete_offer(conn: sqlite3.Connection, offer_id: str) -> bool:
    """Remove a gift; True only for the caller that removed it (the caller commits)."""
    return conn.execute("DELETE FROM ownership_offers WHERE id = ?", (offer_id,)).rowcount == 1


def _with_items(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT object_type, object_id FROM ownership_offer_items WHERE offer_id = ?"
        " ORDER BY object_type, object_id",
        (row["id"],),
    )
    data = dict(row)
    data["with_notes"] = bool(data["with_notes"])
    data["items"] = [{"type": i["object_type"], "id": i["object_id"]} for i in items]
    return data
