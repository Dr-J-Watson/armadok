"""Rules of ownership shared by the routes: who owns what, who may give, what a gift moves."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import Any

from fastapi import Request

from armadok.core import audit
from armadok.core.errors import ApiError
from armadok.core.grants import load_grants
from armadok.core.module import OwnershipChange
from armadok.core.registry import Registry
from armadok.core.rights import Grant, Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.ownership import repository
from armadok.modules.roles.service import atomic

log = logging.getLogger("armadok.ownership")
RELEASE_SECONDS = 60

# What an owner may do with the object itself, on top of the kind's own rights.
GIVE = "ownership.give"
# What the node's owner may do to everything inside it.
MANAGE = "ownership.manage"

# An id becomes a path segment of a scope ("node:a/project:blog"): no slash, no surprise.
OBJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_PENDING_OFFERS = 50
# Gifts waiting for one receiver, so that a crowd of senders cannot bury somebody.
MAX_INBOX = 50
# The most objects of a kind a listing may hold (the worker cuts its lists at 500).
MAX_LISTED = 500


def rights_by_type(registry: Registry) -> dict[str, tuple[str, ...]]:
    """Return what owning an object of each kind gives: the kind's rights, and the right to give."""
    return {t: (*dict.fromkeys((*o.rights, GIVE)),) for t, o in registry.ownables().items()}


def grant_source(
    registry: Registry, conn: sqlite3.Connection, account_id: str
) -> tuple[Grant, ...]:
    """Grants an account holds because it owns objects (see `Module.registry_grant_sources`).

    Owning an object counts only while the person still sees its node: a member who was
    removed and invited again later does not find an old object's rights waiting for them.
    Judged from their own and their roles' grants only, so this cannot loop back into itself.
    """
    base = Principal(account_id, load_grants(conn, account_id))
    catalogue = registry.rights()
    return tuple(
        g
        for g in repository.grants_of(conn, account_id, rights_by_type(registry))
        if base.has("nodes.read", catalogue, g.scope.split("/", 1)[0])
    )


def scope_of(node_id: str, object_type: str, object_id: str) -> str:
    """Return the scope of one object inside a node."""
    return f"node:{node_id}/{object_type}:{object_id}"


def check_object(registry: Registry, object_type: str, object_id: str) -> None:
    """Refuse a kind nobody declared or an id that cannot be an object name."""
    if object_type not in registry.ownables() or not OBJECT_ID.fullmatch(object_id):
        raise ApiError("ownership.unknown_object", 422)


async def existing_ids(
    request: Request, registry: Registry, node_id: str, object_type: str
) -> set[str]:
    """Return the ids that exist for a kind, as its worker says (untrusted: bad ids dropped)."""
    ids = await registry.ownables()[object_type].list_ids(request, registry, node_id)
    if len(ids) >= MAX_LISTED:
        # The worker cuts long lists: "everything" would silently miss some objects.
        raise ApiError("ownership.list_truncated", 409)
    return {i for i in ids if isinstance(i, str) and OBJECT_ID.fullmatch(i)}


async def owned_objects(
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    node: dict[str, Any],
    who: Principal,
    only_type: str | None = None,
) -> list[tuple[str, str]]:
    """Return every object of a node that `person_id` owns.

    Those with a row of their own are read from the database. The node's owner also owns
    everything without a row, which only the node can list (that needs its worker): that
    is asked only for a person's own session holding the right to give, never for a key that
    merely reads the node.
    """
    rows = repository.rows_of_node(conn, node["id"])
    found = {k for k, owner in rows.items() if owner == who.id}
    scope = f"node:{node['id']}"
    if (
        node["owner"] == who.id
        and who.kind == "session"
        and who.has(GIVE, registry.rights(), scope)
    ):
        for kind in registry.ownables():
            if only_type and kind != only_type:
                continue
            for object_id in await existing_ids(request, registry, node["id"], kind):
                if (kind, object_id) not in rows:
                    found.add((kind, object_id))
    return sorted(k for k in found if not only_type or k[0] == only_type)


def move(
    conn: sqlite3.Connection,
    registry: Registry,
    node: dict[str, Any],
    item: tuple[str, str],
    old_owner: str,
    new_owner: str,
    with_notes: bool,
) -> None:
    """Hand one object to a new owner and tell the modules that keep things attached to it.

    The node's owner is the default, so giving an object to them just forgets its row. Runs
    inside the caller's transaction.
    """
    kind, object_id = item
    if new_owner == node["owner"]:
        repository.clear_owner(conn, node["id"], kind, object_id)
    else:
        repository.set_owner(conn, node["id"], kind, object_id, new_owner)
    change = OwnershipChange(node["id"], kind, object_id, old_owner, new_owner, with_notes)
    for listener in registry.ownership_listeners():
        listener(conn, change)


def drop_pending(conn: sqlite3.Connection, node_id: str, items: list[tuple[str, str]]) -> None:
    """Withdraw the gifts that name these objects, and the gifts left with nothing to give."""
    conn.executemany(
        "DELETE FROM ownership_offer_items WHERE object_type = ? AND object_id = ?"
        " AND offer_id IN (SELECT id FROM ownership_offers WHERE node_id = ?)",
        [(kind, object_id, node_id) for kind, object_id in items],
    )
    conn.execute(
        "DELETE FROM ownership_offers WHERE node_id = ? AND id NOT IN"
        " (SELECT offer_id FROM ownership_offer_items)",
        (node_id,),
    )


def _sees(registry: Registry, conn: sqlite3.Connection, user_id: str, node_id: str) -> bool:
    """Return True if the person's own grants and roles let them read the node.

    Whether the account is active is not asked: a disabled owner keeps their objects until they
    lose access for real, and get them back when the account is enabled again.
    """
    base = Principal(user_id, load_grants(conn, user_id))
    return base.has("nodes.read", registry.rights(), f"node:{node_id}")


def release_lost(registry: Registry, conn: sqlite3.Connection) -> int:
    """Give back to the node's owner the objects of people who no longer see the node.

    Without this, being invited again later would bring old objects (and their rights) back to
    life. Returns how many objects moved. Runs after a role is removed and, for the other ways
    of losing access, every minute.
    """
    pairs = conn.execute(
        "SELECT DISTINCT o.node_id, o.owner_id FROM object_owners o"
        " JOIN nodes n ON n.id = o.node_id WHERE n.deleted_at IS NULL"
    ).fetchall()
    gone = [(p["node_id"], p["owner_id"]) for p in pairs if not _sees(registry, conn, p[1], p[0])]
    moved = 0
    for node_id, owner_id in gone:
        with atomic(conn):
            node = nodes_repo.get_node(conn, node_id)
            if node is None or _sees(registry, conn, owner_id, node_id):
                continue
            for item in repository.owned_by(conn, node_id, owner_id):
                move(conn, registry, node, item, owner_id, node["owner"], False)
                moved += 1
            audit.record(
                conn,
                registry.audit_events(),
                "system",
                "ownership.release",
                object_type="node",
                object_id=node_id,
                details={"former_owner": owner_id},
                commit=False,
            )
    return moved


async def watch_loop(app: Any) -> None:
    """Every minute, release the objects of people who lost the node by any other route."""
    while True:
        await asyncio.sleep(RELEASE_SECONDS)
        conn = app.state.settings.open_db()
        try:
            release_lost(app.state.registry, conn)
        except Exception:  # noqa: BLE001  (one bad round must not end the loop)
            log.exception("could not release lost objects")
        finally:
            conn.close()


def can_see_node(registry: Registry, conn: sqlite3.Connection, user_id: str, node_id: str) -> bool:
    """Return True if an active, unblocked person holds `nodes.read` on the node."""
    if registry.account_blocked(conn, user_id):
        return False
    who: Principal = registry.principal_for(conn, user_id, kind="background")
    return who.has("nodes.read", registry.rights(), f"node:{node_id}")
