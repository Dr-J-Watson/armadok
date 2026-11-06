"""Database access for nodes. Rights and audit are handled by the routes, not here."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from armadok.core.db import Migration

MIGRATION = Migration(
    "nodes-0001-create",
    """
    CREATE TABLE nodes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('network', 'docker', 'full')),
        description TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    """,
)

LOCAL_MIGRATION = Migration(
    "nodes-0002-local",
    """
    ALTER TABLE nodes ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0;
    -- At most one node is the hub's own node.
    CREATE UNIQUE INDEX nodes_one_local ON nodes (is_local) WHERE is_local = 1;
    """,
)

SUSPEND_MIGRATION = Migration(
    "nodes-0003-suspend",
    """
    ALTER TABLE nodes ADD COLUMN suspended_at TEXT;
    ALTER TABLE nodes ADD COLUMN suspended_reason TEXT;
    """,
)

# Deleted nodes can be restored for this long, then they may be purged.
RESTORE_DAYS = 30

_COLUMNS = (
    "id, name, type, description, owner, created_at, is_local AS local,"
    " suspended_at IS NOT NULL AS suspended, suspended_reason"
)


def _row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["local"] = bool(data["local"])
    data["suspended"] = bool(data["suspended"])
    return data


def suspended_scopes(conn: sqlite3.Connection) -> frozenset[str]:
    """Return the scopes of the suspended nodes: nobody may use them (see `Principal.blocked`)."""
    rows = conn.execute(
        "SELECT id FROM nodes WHERE suspended_at IS NOT NULL AND deleted_at IS NULL"
    )
    return frozenset(f"node:{r['id']}" for r in rows)


def set_suspended(conn: sqlite3.Connection, node_id: str, reason: str | None) -> None:
    """Suspend a node (with the reason) or, with `reason` None, lift the suspension."""
    if reason is None:
        conn.execute(
            "UPDATE nodes SET suspended_at = NULL, suspended_reason = NULL WHERE id = ?",
            (node_id,),
        )
    else:
        conn.execute(
            "UPDATE nodes SET suspended_at = ?, suspended_reason = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), reason, node_id),
        )
    conn.commit()


def list_all_for_admin(conn: sqlite3.Connection) -> list[dict]:
    """Return every node, deleted ones included, with only what an administrator may see.

    Name, type, owner and state; never any content of the node.
    """
    rows = conn.execute(
        "SELECT n.id, n.name, n.type, n.owner, n.created_at, n.deleted_at, n.suspended_at,"
        " n.suspended_reason, n.is_local, u.username AS owner_name"
        " FROM nodes n LEFT JOIN users u ON u.id = n.owner ORDER BY n.created_at, n.id"
    )
    return [dict(r) for r in rows]


def get_any(conn: sqlite3.Connection, node_id: str) -> dict | None:
    """Return a node whether or not it is deleted (administration only)."""
    row = conn.execute(
        "SELECT id, name, owner, deleted_at, suspended_at, is_local FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


def set_owner(conn: sqlite3.Connection, node_id: str, owner_id: str) -> None:
    """Record who owns a node (their rights are moved by the caller)."""
    conn.execute("UPDATE nodes SET owner = ? WHERE id = ?", (owner_id, node_id))


def restore(conn: sqlite3.Connection, node_id: str) -> None:
    """Bring a deleted node back."""
    conn.execute("UPDATE nodes SET deleted_at = NULL WHERE id = ?", (node_id,))
    conn.commit()


def list_nodes(conn: sqlite3.Connection) -> list[dict]:
    """Return all non-deleted nodes, oldest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM nodes WHERE deleted_at IS NULL ORDER BY created_at, id"
    )
    return [_row(r) for r in rows]


def get_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    """Return one non-deleted node, or None."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM nodes WHERE id = ? AND deleted_at IS NULL", (node_id,)
    ).fetchone()
    return _row(row) if row else None


def create_node(
    conn: sqlite3.Connection, name: str, node_type: str, description: str, owner: str
) -> dict:
    """Insert a node and return it."""
    node_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO nodes (id, name, type, description, owner, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, name, node_type, description, owner, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return get_node(conn, node_id)  # type: ignore[return-value]


def update_node(conn: sqlite3.Connection, node_id: str, changes: dict[str, str]) -> None:
    """Apply `changes` (name, type, description only) to a node."""
    allowed = {"name", "type", "description"}
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE nodes SET {assignments} WHERE id = ?", (*fields.values(), node_id))
    conn.commit()


def soft_delete_node(conn: sqlite3.Connection, node_id: str) -> None:
    """Mark a node deleted; it stays recoverable (purge after 30 days is a later task)."""
    conn.execute(
        "UPDATE nodes SET deleted_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), node_id)
    )
    conn.commit()


def get_local(conn: sqlite3.Connection) -> dict | None:
    """Return the node that stands for the hub's own node, if there is one."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM nodes WHERE is_local = 1 AND deleted_at IS NULL"
    ).fetchone()
    return _row(row) if row else None


def create_local(conn: sqlite3.Connection) -> dict:
    """Create the local node (a full node: Docker and Traefik on the hub's node)."""
    node_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO nodes (id, name, type, description, owner, created_at, is_local)"
        " VALUES (?, 'Local', 'full', 'The node Armadok runs on.', 'system', ?, 1)",
        (node_id, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return get_node(conn, node_id)  # type: ignore[return-value]


def purge(
    conn: sqlite3.Connection,
    node_id: str,
    tables: tuple[tuple[str, str], ...] = (),
    record: Callable[[], None] | None = None,
) -> bool:
    """Erase a deleted node for good, in one transaction; return False if it was not erasable.

    Only a node that is deleted (and not the hub's own) goes, checked under the write lock, so a
    restore that lands first wins. Tables that reference the node with a cascade go with it;
    grants and assignments carry the node only in their `scope` text; `tables` are the other
    tables modules declared (`Module.node_tables`): rows deleted, or `node_id` emptied.
    Table names come from module declarations, never from a request. `record` (an audit entry,
    say) runs inside the same transaction, so the log and the erasure stand or fall together.
    """
    scope = f"node:{node_id}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        gone = conn.execute(
            "DELETE FROM nodes WHERE id = ? AND deleted_at IS NOT NULL AND is_local = 0",
            (node_id,),
        ).rowcount
        if gone:
            if record is not None:
                record()
            conn.execute("DELETE FROM grants WHERE scope = ?", (scope,))
            conn.execute("DELETE FROM assignments WHERE scope = ?", (scope,))
            for table, action in tables:
                if action == "delete":
                    conn.execute(f"DELETE FROM {table} WHERE node_id = ?", (node_id,))  # noqa: S608
                else:
                    conn.execute(  # noqa: S608
                        f"UPDATE {table} SET node_id = NULL WHERE node_id = ?", (node_id,)
                    )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return bool(gone)


def purgeable(conn: sqlite3.Connection) -> list[str]:
    """Return the ids of nodes deleted for longer than the restore window."""
    limit = (datetime.now(UTC) - timedelta(days=RESTORE_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id FROM nodes WHERE deleted_at IS NOT NULL AND deleted_at <= ? AND is_local = 0",
        (limit,),
    )
    return [r["id"] for r in rows]
