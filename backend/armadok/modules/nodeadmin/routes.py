"""Quick actions of a platform administrator on nodes.

An administrator sees that nodes exist (name, owner, state) and can suspend, resume or restore
them; they never see or touch what is on a node (that stays with its owner, or needs an
emergency session). Each action needs a fresh second-factor code and typing the node's name, and
is audited with its reason.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.core.signals import announce
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.roles import repository as roles_repo
from armadok.modules.users import second_factor

router = APIRouter(prefix="/admin/nodes", tags=["admin"])

NodeId = Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$")


class Confirm(BaseModel):
    """What every quick action asks for."""

    model_config = ConfigDict(extra="forbid")
    confirm_name: str = Field(max_length=100)
    code: str = Field(max_length=30)


class SuspendIn(Confirm):
    """Suspension also needs a reason, which the owner will read."""

    reason: str = Field(min_length=10, max_length=300)


def _admin(who: Principal, registry: Registry) -> None:
    if who.kind != "session":
        raise MissingRight("nodeadmin.not_with_key")
    who.require("nodes.admin", registry.rights())


def _audit(conn, registry, who, event, node_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        details=details,
    )


def _checked(
    request: Request, conn, registry, who: Principal, node_id: str, body: Confirm, event: str
) -> dict:
    """Check the node exists, its name was typed and a fresh 2FA code is valid; return the node."""
    node = nodes_repo.get_any(conn, node_id)
    if node is None:
        raise NotFound()
    try:
        # The name first: a typo must not burn a one-time code.
        if body.confirm_name != node["name"]:
            raise ApiError("nodeadmin.confirm_mismatch", 422)
        second_factor.require_fresh_code(conn, request.app.state.settings, who.id, body.code)
    except ApiError as error:
        # A refused attempt leaves a trace (a flood of wrong codes is already cut short).
        if error.code != "auth.too_many_attempts":
            audit.record(
                conn,
                registry.audit_events(),
                who.actor,
                event,
                object_type="node",
                object_id=node_id,
                outcome="denied",
                details={"code": error.code},
            )
        raise
    return node


async def _tell(request: Request, node_id: str) -> None:
    """Tell open screens (the owner's included) that a node changed state."""
    await announce(request.app, "nodes.changed", node_id)
    await announce(request.app, "nodes.changed", None)


def _state(node: dict) -> str:
    if node["deleted_at"]:
        return "deleted"
    return "suspended" if node["suspended_at"] else "active"


@router.get("", summary="Every node, by name, owner and state")
def list_nodes(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return all nodes (deleted ones too) with no content, for administration."""
    _admin(who, registry)
    out = []
    for n in nodes_repo.list_all_for_admin(conn):
        deleted = n["deleted_at"]
        out.append(
            {
                "id": n["id"],
                "name": n["name"],
                "type": n["type"],
                "owner": n["owner_name"] or n["owner"],
                "local": bool(n["is_local"]),
                "state": _state(n),
                "suspended_reason": n["suspended_reason"],
                "restorable_until": (
                    (
                        datetime.fromisoformat(deleted) + timedelta(days=nodes_repo.RESTORE_DAYS)
                    ).isoformat()
                    if deleted
                    else None
                ),
            }
        )
    return out


@router.get("/{node_id}/impact", summary="What suspending a node would affect")
def impact(
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return counts only: members, and whether its worker is connected."""
    _admin(who, registry)
    node = nodes_repo.get_any(conn, node_id)
    if node is None:
        raise NotFound()
    scope = f"node:{node_id}"
    hub = getattr(request.app.state, "worker_hub", None)
    return {
        "members": sum(1 for a in roles_repo.list_assignments(conn) if a["scope"] == scope),
        "worker_connected": bool(hub and hub.get(node_id)),
    }


@router.post("/{node_id}/suspend", summary="Suspend a node")
async def suspend(
    body: SuspendIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Refuse every access to the node (its owner is told why), keeping its node as it is."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.suspend")
    if node["deleted_at"] or node["suspended_at"]:
        raise ApiError("nodeadmin.wrong_state", 409)
    if node["is_local"]:
        raise ApiError("nodeadmin.local_not_suspendable", 409)  # it would cut the hub itself
    nodes_repo.set_suspended(conn, node_id, body.reason)
    await _tell(request, node_id)
    _audit(conn, registry, who, "node.suspend", node_id, reason=body.reason)
    return {"state": "suspended"}


@router.post("/{node_id}/resume", summary="Lift the suspension of a node")
async def resume(
    body: Confirm,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give the node back to its members."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.resume")
    if node["deleted_at"] or not node["suspended_at"]:
        raise ApiError("nodeadmin.wrong_state", 409)
    nodes_repo.set_suspended(conn, node_id, None)
    await _tell(request, node_id)
    _audit(conn, registry, who, "node.resume", node_id)
    return {"state": "active"}


@router.post("/{node_id}/restore", summary="Bring back a deleted node")
async def restore(
    body: Confirm,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Restore a node deleted less than 30 days ago, as it was."""
    _admin(who, registry)
    node = _checked(request, conn, registry, who, node_id, body, "node.restore")
    if not node["deleted_at"]:
        raise ApiError("nodeadmin.wrong_state", 409)
    limit = datetime.fromisoformat(node["deleted_at"]) + timedelta(days=nodes_repo.RESTORE_DAYS)
    if limit <= datetime.now(UTC):
        raise ApiError("nodeadmin.restore_expired", 410)
    nodes_repo.restore(conn, node_id)
    await _tell(request, node_id)
    _audit(conn, registry, who, "node.restore", node_id)
    return {"state": "active"}
