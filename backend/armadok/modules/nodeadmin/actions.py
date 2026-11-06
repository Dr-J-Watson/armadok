"""The other quick actions of a platform administrator: cut the worker, delete, purge, take over.

Same rules as the first ones (see `routes`): a real session, `nodes.admin`, the node's name
typed and a fresh second-factor code, every attempt audited. Each action that changes who can
reach a node also asks for a reason, which is kept in the audit log. None of them reads or
changes what is on the node; taking ownership is the one that gives somebody access, so it
names the new owner explicitly and leaves the former one without any right on the node.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from armadok.core import audit, grants
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.events import emit
from armadok.core.registry import Registry
from armadok.core.rights import Grant, Principal, can_grant
from armadok.core.throttle import FailureLimiter
from armadok.modules.nodeadmin.routes import (
    Confirm,
    NodeId,
    _admin,
    _checked,
    _tell,
)
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.roles.service import atomic
from armadok.modules.service_accounts import repository as service_accounts
from armadok.modules.users import repository as users_repo
from armadok.modules.workers.routes import on_node_deleted

router = APIRouter(prefix="/admin/nodes", tags=["admin"])

# Looking people up by name is for choosing a new owner, not for listing the directory.
search_limiter = FailureLimiter({"user": 30})


class ReasonIn(Confirm):
    """An action that changes who can reach a node says why."""

    reason: str = Field(min_length=10, max_length=300)


class TakeOverIn(ReasonIn):
    """Taking ownership names the person who becomes the owner."""

    user_id: str = Field(max_length=64)


def _owner(conn: sqlite3.Connection, node: dict) -> dict:
    """Return the owner's account, or raise 404 when the node has none."""
    owner = users_repo.get_user(conn, node["owner"]) if node["owner"] else None
    if owner is None:
        raise NotFound()
    return owner


def _record(conn, registry, who, event, node_id, *, commit=True, **details) -> None:
    """Write the audit entry of an action; with `commit=False` it lands with the change."""
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        details=details,
        commit=commit,
    )


async def _tell_people(request: Request, event: str, people: list[str], **params: str) -> None:
    """Tell the named people (not the node's members) what an administrator did."""
    await emit(request.app, "notify", event=event, node_id=None, params=params, user_ids=people)


def _platform_outranks(conn, who: Principal, user_id: str, registry: Registry) -> bool:
    """Tell whether `who` could have given every platform-wide grant of the target.

    Rights on the target's own nodes are left out: an administrator holds none of them (no
    implicit access), which must not stop them from acting on the account.
    """
    catalogue = registry.rights()
    now = datetime.now(UTC)
    for g in registry.grants_of(conn, user_id):
        if g.scope.startswith("node:") or (g.expires_at is not None and g.expires_at <= now):
            continue
        if not can_grant(who, g, catalogue):
            return False
    return True


@router.get("/people/search", summary="Find an active person by user name")
def find_people(
    q: str = Query(min_length=2, max_length=50),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return up to 10 active people whose user name starts with `q` (id and name only).

    For choosing the new owner in a take-over; it shows nothing about what they own, and the
    number of searches is limited so it cannot be used to list the directory.
    """
    _admin(who, registry)
    if search_limiter.blocked(user=who.id):
        raise ApiError("members.too_many_searches", 429)
    search_limiter.remember(user=who.id)
    pattern = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        "SELECT id, username FROM users WHERE username LIKE ? ESCAPE '\\' AND is_active = 1"
        " AND kind = 'person' ORDER BY username LIMIT 10",
        (pattern,),
    )
    return [dict(r) for r in rows]


@router.post("/{node_id}/disconnect", summary="Cut the worker of a node")
async def disconnect(
    body: ReasonIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Revoke the worker and close its connection; the owner enrols a new one when ready."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.disconnect")
    if node["is_local"]:
        raise ApiError("nodeadmin.local_not_suspendable", 409)  # it is the hub itself
    _record(conn, registry, who, "node.disconnect", node_id, reason=body.reason)
    # The same effect as a deletion on the node's side, without touching the node record.
    await on_node_deleted(request.app, node_id)
    await _tell(request, node_id)
    if node["owner"]:
        await _tell_people(request, "nodeadmin.disconnected", [node["owner"]], node=node["name"])
    return {"state": "disconnected"}


@router.post("/{node_id}/delete", summary="Delete a node (recoverable for 30 days)")
async def delete(
    body: ReasonIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Hide the node and cut its worker. Nothing is changed on the node itself."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.admin_delete")
    if node["deleted_at"]:
        raise ApiError("nodeadmin.wrong_state", 409)
    if node["is_local"]:
        raise ApiError("nodes.local_not_deletable", 409)
    # The entry and the change are committed together (the deletion commits what is pending).
    _record(conn, registry, who, "node.admin_delete", node_id, commit=False, reason=body.reason)
    nodes_repo.soft_delete_node(conn, node_id)
    await emit(request.app, "node.deleted", node_id=node_id)
    await _tell(request, node_id)
    if node["owner"]:
        await _tell_people(request, "nodeadmin.deleted", [node["owner"]], node=node["name"])
    return {"state": "deleted"}


@router.post("/{node_id}/purge", summary="Erase a deleted node for good")
async def purge(
    body: ReasonIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Erase a node that was already deleted. It cannot be undone, hence the extra step."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.purge")
    if not node["deleted_at"] or node["is_local"]:
        raise ApiError("nodeadmin.wrong_state", 409)

    def record() -> None:
        _record(
            conn, registry, who, "node.purge", node_id, commit=False,
            reason=body.reason, name=node["name"],
        )  # fmt: skip

    try:
        done = nodes_repo.purge(conn, node_id, registry.node_tables(), record)
    except sqlite3.IntegrityError:
        raise ApiError("nodeadmin.purge_blocked", 409) from None
    if not done:
        raise ApiError("nodeadmin.wrong_state", 409)  # restored (or gone) in the meantime
    await _tell(request, node_id)
    return {"state": "purged"}


@router.post("/{node_id}/take-over", summary="Give the node to another owner")
async def take_over(
    body: TakeOverIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Take the node from its owner (who loses every right on it) and name the new one.

    For an owner who cannot or must not keep it (banned, gone). The new owner is another active
    person, never the administrator who does this: an administrator has no implicit access to
    nodes, and this must not be a way to get it. The node's worker is revoked, so the new
    owner enrols one on purpose, and both owners are told.
    """
    _admin(who, registry)
    _checked(request, conn, registry, who, node_id, body, "node.takeover")
    if body.user_id == who.id:
        raise ApiError("nodeadmin.takeover_self", 409)
    scope = f"node:{node_id}"
    owner_rights = registry.node_owner_rights()
    with atomic(conn):
        # Everything that decides is read again under the write lock.
        node = nodes_repo.get_any(conn, node_id)
        target = users_repo.get_user(conn, body.user_id)
        if node is None or node["deleted_at"] or node["is_local"]:
            raise ApiError("nodeadmin.wrong_state", 409)
        if target is None or not target["is_active"] or target["kind"] != "person":
            raise NotFound()
        if registry.account_blocked(conn, target["id"]) or target["id"] == node["owner"]:
            raise ApiError("nodeadmin.wrong_state", 409)
        former = node["owner"]
        _record(
            conn, registry, who, "node.takeover", node_id, commit=False,
            reason=body.reason, to=target["id"], was=former,
        )  # fmt: skip
        for g in grants.list_grants(conn, target["id"]):
            if g["scope"] == scope and g["right"] in owner_rights:
                conn.execute("DELETE FROM grants WHERE id = ?", (g["id"],))
        for right in owner_rights:
            grants.add_grant(conn, target["id"], Grant(right, scope), who.id, commit=False)
        if former:
            # The former owner keeps nothing on this node, nor do the accounts they run.
            holders = [former, *(a["id"] for a in service_accounts.list_for(conn, former))]
            for holder in holders:
                conn.execute(
                    "DELETE FROM grants WHERE principal_id = ? AND scope = ?", (holder, scope)
                )
                conn.execute(
                    "DELETE FROM assignments"
                    " WHERE subject_type = 'user' AND subject_id = ? AND scope = ?",
                    (holder, scope),
                )
        nodes_repo.set_owner(conn, node_id, target["id"])
    # No worker carries over: the new owner decides what runs on their node.
    await on_node_deleted(request.app, node_id)
    await _tell(request, node_id)
    people = [target["id"], *([former] if former else [])]
    await _tell_people(request, "nodeadmin.owner_changed", people, node=node["name"])
    return {"owner": target["id"]}


@router.post("/{node_id}/ban-owner", summary="Disable the account of the owner")
async def ban_owner(
    body: ReasonIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Disable the owner's account and end its sessions. Their nodes are not touched.

    The same rules as disabling an account from the account screen apply (`users.update`, and
    only accounts whose rights you could grant), and an account that holds any sensitive right is
    refused outright: taking an administrator out is not a quick action, and this must not be a
    way to lock the last one out.
    """
    _admin(who, registry)
    who.require("users.update", registry.rights())
    node = _checked(request, conn, registry, who, node_id, body, "node.owner_ban")
    owner = _owner(conn, node)
    holder = registry.principal_for(conn, owner["id"], kind="background")
    catalogue = registry.rights()
    if owner["id"] == who.id or any(
        r.sensitive and holder.has(r.id, catalogue) for r in catalogue.values()
    ):
        raise ApiError("nodeadmin.owner_is_admin", 409)
    if not _platform_outranks(conn, who, owner["id"], registry):
        raise MissingRight()
    _record(
        conn, registry, who, "node.owner_ban", node_id, commit=False,
        reason=body.reason, owner=owner["id"],
    )  # fmt: skip
    users_repo.set_active(conn, owner["id"], False)  # commits the entry with the change
    await _tell(request, node_id)
    return {"banned": owner["id"]}


@router.post("/{node_id}/strip-creation", summary="Take the right to create nodes from the owner")
async def strip_creation(
    body: ReasonIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Remove the owner's right to create nodes (the nodes they already own are kept)."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.creation_removed")
    owner = _owner(conn, node)
    with atomic(conn):
        _record(
            conn, registry, who, "node.creation_removed", node_id, commit=False,
            reason=body.reason, owner=owner["id"],
        )  # fmt: skip
        cur = conn.execute(
            "DELETE FROM grants WHERE principal_id = ? AND right = 'nodes.create'", (owner["id"],)
        )
    return {"removed": cur.rowcount}
