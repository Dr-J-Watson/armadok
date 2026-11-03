"""Emergency access endpoints.

For the rare case where an administrator must get into a node that is not theirs (the owner is
gone, something is on fire). It needs a real session, a written reason (10 characters or more),
a fresh second-factor code, and lasts 30 minutes with the node's administrator rights; it can be
ended at any moment. The owner is told at once and keeps a notice they can acknowledge but never
erase, listing what was done. Nothing here applies to keys, tokens or hooks.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.events import emit
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.core.signals import announce
from armadok.modules.emergency import repository as repo
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.users import second_factor

router = APIRouter(tags=["emergency"])

NodeId = Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
SessionId = Path(pattern=r"^[0-9a-f]{32}$")
MAX_ACTIONS = 200  # shown per session: the first half and the last half if there are more


class StartIn(BaseModel):
    """Why, and a fresh second-factor code."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=10, max_length=300)
    code: str = Field(max_length=30)


def _audit(conn, registry, who, event, node_id, outcome="success", **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        outcome=outcome,
        details=details,
    )


def _owner_gate(
    conn, registry, who: Principal, node_id: str, *, reading: bool = False
) -> dict | None:
    """Check who may see or act on this node's emergency notices, even if it is suspended.

    The owner (anyone holding the notices right there) may read, acknowledge and end sessions.
    With `reading`, a holder of `audit.read` everywhere may also read them, which keeps the
    notices reachable when the owner is gone or the node was deleted. Returns the live node,
    or None for a deleted one that only an auditor reads.
    """
    node = nodes_repo.get_node(conn, node_id)
    scope = f"node:{node_id}"
    catalogue = registry.rights()
    if node is not None and who.has("nodes.read", catalogue, scope, ignore_blocked=True):
        if who.has("emergency.notices", catalogue, scope, ignore_blocked=True):
            return node
        if not (reading and who.has("audit.read", catalogue)):
            raise MissingRight()
        return node
    if reading and who.has("audit.read", catalogue) and nodes_repo.get_any(conn, node_id):
        return None
    raise NotFound()


def _refuse(conn, registry, who: Principal, node_id: str, error: Exception) -> None:
    """Audit a refused attempt to open a session, then let the error go on."""
    code = getattr(error, "code", "rights.missing")
    _audit(conn, registry, who, "emergency.start", node_id, "denied", code=code)
    raise error


@router.post(
    "/admin/nodes/{node_id}/emergency", status_code=201, summary="Open an emergency session"
)
async def open_session(
    body: StartIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Open a 30-minute session with the node's administrator rights; the owner is told."""
    try:
        if who.kind != "session":
            raise MissingRight("emergency.not_with_key")
        who.require("emergency.use", registry.rights())
    except (MissingRight, ApiError) as error:
        _refuse(conn, registry, who, node_id, error)
    node = nodes_repo.get_any(conn, node_id)
    if node is None or node["deleted_at"]:
        raise NotFound()
    # Everything that can be refused without spending the code comes first.
    try:
        if node["owner"] == who.id:
            raise ApiError("emergency.own_node", 409)  # it is yours already
        if any(s["node_id"] == node_id for s in repo.active_for(conn, who.id)):
            raise ApiError("emergency.already_open", 409)
        if repo.cooling_down(conn, who.id, node_id):
            raise ApiError("emergency.cooling_down", 429)  # the owner just cut you off
    except ApiError as error:
        _refuse(conn, registry, who, node_id, error)
    try:
        second_factor.require_fresh_code(conn, request.app.state.settings, who.id, body.code)
    except ApiError as error:
        if error.code != "auth.too_many_attempts":
            _audit(conn, registry, who, "emergency.start", node_id, "denied", code=error.code)
        raise
    session = repo.start(conn, who.id, node_id, body.reason)
    _audit(
        conn, registry, who, "emergency.start", node_id, reason=body.reason, session=session["id"]
    )
    # The owner learns of it at once, by notification and on screen.
    await emit(
        request.app,
        "notify",
        event="emergency.started",
        node_id=node_id,
        params={"node": node["name"]},
    )
    await announce(request.app, "emergency.changed", node_id)
    return {"id": session["id"], "expires_at": session["expires_at"]}


@router.delete(
    "/admin/nodes/{node_id}/emergency", status_code=204, summary="End your emergency session"
)
async def close_session(
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """End your open session on this node at once."""
    mine = [s for s in repo.active_for(conn, who.id) if s["node_id"] == node_id]
    if not mine:
        raise NotFound()
    for s in mine:
        repo.end(conn, s["id"], who.id)
    _audit(conn, registry, who, "emergency.end", node_id, session=mine[0]["id"])
    await announce(request.app, "emergency.changed", node_id)


@router.delete(
    "/nodes/{node_id}/emergency/{session_id}",
    status_code=204,
    summary="Cut an emergency session short",
)
async def cut_session(
    request: Request,
    node_id: str = NodeId,
    session_id: str = SessionId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """End someone's open session on your node at once. The notice stays, and so does the end."""
    _owner_gate(conn, registry, who, node_id)
    found = repo.get(conn, session_id)
    if found is None or found["node_id"] != node_id or found["ended_at"]:
        raise NotFound()
    repo.end(conn, session_id, who.id)
    _audit(conn, registry, who, "emergency.cut", node_id, session=session_id)
    await announce(request.app, "emergency.changed", node_id)


@router.get("/emergency/active", summary="Your open emergency sessions")
def active(
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the emergency sessions you have open (for the permanent banner)."""
    out = []
    for s in repo.active_for(conn, who.id):
        node = nodes_repo.get_any(conn, s["node_id"])
        out.append(
            {
                "id": s["id"],
                "node_id": s["node_id"],
                "node_name": node["name"] if node else s["node_id"],
                "expires_at": s["expires_at"],
            }
        )
    return out


def _what_was_done(conn, session: dict, node_id: str, end: str) -> tuple[list[dict], int]:
    """Return what the administrator did during a session: first and last entries, and the count.

    Anything the audit log has for that person in that time that names this node, whatever the
    kind of object. If there are more than `MAX_ACTIONS`, the owner gets the beginning and the
    end (and the true total), so a flood in the middle cannot push the telling entries out.
    """
    where = (
        "actor LIKE ? ESCAPE '\\' AND at >= ? AND at <= ? AND event != 'emergency.start'"
        " AND ((object_type = 'node' AND object_id = ?) OR details LIKE ?)"
    )
    args = (session["admin_id"] + "%", session["started_at"], end, node_id, f"%{node_id}%")
    total = conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where}", args).fetchone()[0]
    select = f"SELECT id, at, event, outcome FROM audit_log WHERE {where}"
    if total <= MAX_ACTIONS:
        rows = conn.execute(f"{select} ORDER BY id", args).fetchall()
    else:
        half = MAX_ACTIONS // 2
        first = conn.execute(f"{select} ORDER BY id LIMIT ?", (*args, half)).fetchall()
        last = conn.execute(f"{select} ORDER BY id DESC LIMIT ?", (*args, half)).fetchall()
        rows = [*first, *reversed(last)]
    return [{"at": r["at"], "event": r["event"], "outcome": r["outcome"]} for r in rows], total


@router.get("/nodes/{node_id}/emergency", summary="Emergency accesses to this node")
def notices(
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return every emergency session on the node with what the administrator did during it."""
    _owner_gate(conn, registry, who, node_id, reading=True)
    now = datetime.now(UTC).isoformat()
    out = []
    for s in repo.for_node(conn, node_id):
        end = s["ended_at"] or min(s["expires_at"], now)
        actions, total = _what_was_done(conn, s, node_id, end)
        out.append(
            {
                "id": s["id"],
                "admin": s["admin_name"] or s["admin_id"],
                "reason": s["reason"],
                "started_at": s["started_at"],
                "expires_at": s["expires_at"],
                "ended_at": s["ended_at"],
                "ended_by_owner": bool(s["ended_by"] and s["ended_by"] != s["admin_id"]),
                "open": not s["ended_at"] and s["expires_at"] > now,
                "acknowledged": bool(s["acknowledged_at"]),
                "actions": actions,
                "total_actions": total,
                "truncated": total > len(actions),
            }
        )
    return out


@router.post(
    "/nodes/{node_id}/emergency/{session_id}/acknowledge",
    status_code=204,
    summary="Acknowledge an emergency notice",
)
async def acknowledge(
    request: Request,
    node_id: str = NodeId,
    session_id: str = SessionId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Say you have seen it. The notice stays, for good."""
    _owner_gate(conn, registry, who, node_id)
    found = repo.get(conn, session_id)
    if found is None or found["node_id"] != node_id:
        raise NotFound()
    repo.acknowledge(conn, session_id, who.id)
    _audit(conn, registry, who, "emergency.acknowledge", node_id, session=session_id)
    await announce(request.app, "emergency.changed", node_id)
