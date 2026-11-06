"""The local node: the hub's own node, managed without a worker.

Created at start-up when `ARMADOK_LOCAL_NODE=true`. It belongs to the first account (the
super-administrator), who receives the usual owner rights on it, and to nobody else: like any
other node, it is invisible to other accounts until they are given rights on it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from armadok.core.grants import add_grant, list_grants
from armadok.core.registry import Registry
from armadok.core.rights import Grant
from armadok.modules.nodes import repository


def _first_account(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT id FROM users ORDER BY created_at, rowid LIMIT 1").fetchone()
    return row["id"] if row else None


def give_owner_rights(conn: sqlite3.Connection, registry: Registry, user_id: str) -> None:
    """Give `user_id` the owner rights on the local node (idempotent)."""
    node = repository.get_local(conn)
    if node is None:
        return
    scope = f"node:{node['id']}"
    held = {(g["right"], g["scope"]) for g in list_grants(conn, user_id)}
    for right in registry.node_owner_rights():
        if (right, scope) not in held:
            add_grant(conn, user_id, Grant(right, scope), granted_by="system")


def ensure_owner_rights(conn: sqlite3.Connection, settings: Any, registry: Registry) -> None:
    """Start-up hook: every node's owner holds the owner rights of every enabled module.

    A module added after a node was made declares new owner rights; without this, only the
    local node's owner would ever receive them. Idempotent.
    """
    rights = registry.node_owner_rights()
    for node in conn.execute("SELECT id, owner FROM nodes WHERE deleted_at IS NULL").fetchall():
        scope = f"node:{node['id']}"
        held = {g["right"] for g in list_grants(conn, node["owner"]) if g["scope"] == scope}
        for right in rights:
            if right not in held:
                add_grant(conn, node["owner"], Grant(right, scope), granted_by="system")


def ensure_local_node(conn: sqlite3.Connection, settings: Any, registry: Registry) -> None:
    """Start-up hook: create the local node if enabled, and give it to the first account."""
    if not settings.local_node:
        return
    if repository.get_local(conn) is None:
        repository.create_local(conn)
    owner = _first_account(conn)
    if owner:
        give_owner_rights(conn, registry, owner)


async def on_setup_completed(app: Any, user_id: str) -> None:
    """Hand the local node, if there is one, to the account that was just created."""
    conn = app.state.settings.open_db()
    try:
        give_owner_rights(conn, app.state.registry, user_id)
    finally:
        conn.close()
