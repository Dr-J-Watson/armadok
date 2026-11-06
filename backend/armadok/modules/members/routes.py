"""Members of a node (who works on it, with which role) and the transfer of ownership.

A node's owner (or anyone holding `nodes.members` on it) manages its members without any
platform right: a member is a role given on this node only. The rule of the rights stays: you
can give, change or take away only what you hold yourself. Transfer is the owner's own act; it
moves the owner rights to the new owner and leaves the former one as an administrator.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from armadok.core import audit, grants
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Grant, Principal
from armadok.core.throttle import FailureLimiter
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.roles import builtin
from armadok.modules.roles import repository as roles_repo
from armadok.modules.roles.service import atomic, may_give
from armadok.modules.users import repository as users_repo
from armadok.modules.users import second_factor

router = APIRouter(tags=["members"])

# Searches in the directory, per person: it must not be a way to list every account.
DIRECTORY_PER_MINUTE = 30
directory_limiter = FailureLimiter({"user": DIRECTORY_PER_MINUTE})

NodeId = Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
AssignmentId = Path(pattern=r"^[0-9a-f]{32}$")


class MemberIn(BaseModel):
    """A person to give a role on this node."""

    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=64)
    role_id: str = Field(max_length=64)
    expires_at: AwareDatetime | None = None


class MemberPatch(BaseModel):
    """A change of role and/or end date (`null` removes the end date)."""

    model_config = ConfigDict(extra="forbid")
    role_id: str | None = Field(default=None, max_length=64)
    expires_at: AwareDatetime | None = None
    clear_expiry: bool = False


class TransferIn(BaseModel):
    """The person who becomes the owner, the node's name and a fresh second-factor code."""

    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=64)
    confirm_name: str = Field(max_length=100)
    code: str = Field(max_length=30)


def _scope(node_id: str) -> str:
    return f"node:{node_id}"


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


def _gate(conn, registry, who: Principal, node_id: str, right: str) -> tuple[dict, dict]:
    """Return `(node, catalogue)` if the node is visible and `right` is held on it (404 first)."""
    catalogue = registry.rights()
    node = nodes_repo.get_node(conn, node_id)
    if node is None or not who.has("nodes.read", catalogue, _scope(node_id)):
        raise NotFound()
    who.require(right, catalogue, _scope(node_id))
    return node, catalogue


def _member_assignment(conn, node_id: str, assignment_id: str) -> dict:
    found = roles_repo.get_assignment(conn, assignment_id)
    if found is None or found["scope"] != _scope(node_id):
        raise NotFound()
    return found


def _person(conn, user_id: str) -> dict:
    user = users_repo.get_user(conn, user_id)
    if user is None or not user["is_active"] or user["kind"] != "person":
        raise NotFound()
    return user


@router.get("/nodes/{node_id}/members", summary="Members of a node")
def list_members(
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the owner and everyone who was given a role on this node (people and groups)."""
    node, _ = _gate(conn, registry, who, node_id, "nodes.members")
    owner = users_repo.get_user(conn, node["owner"])
    members = [
        {
            "assignment_id": a["id"],
            "subject_type": a["subject_type"],
            "subject_id": a["subject_id"],
            "subject_name": a["subject_name"],
            "role_id": a["role_id"],
            "role_name": a["role_name"],
            "role_builtin": bool(a["role_builtin"]),
            "expires_at": a["expires_at"],
        }
        for a in roles_repo.list_assignments(conn)
        if a["scope"] == _scope(node_id)
    ]
    return {
        "owner": {"id": node["owner"], "name": owner["username"] if owner else node["owner"]},
        "members": members,
    }


@router.get("/nodes/{node_id}/members/roles", summary="Roles that can be given here")
def assignable_roles(
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the roles a manager of this node can pick from (the server still checks each gift).

    Only the roles you could give in full, so the list never offers what would be refused.
    """
    _, catalogue = _gate(conn, registry, who, node_id, "nodes.members")
    scope = _scope(node_id)
    return [
        {"id": r["id"], "name": r["name"], "builtin": r["builtin"]}
        for r in roles_repo.list_roles(conn)
        if all(who.has(right, catalogue, scope) for right in r["rights"])
    ]


@router.post("/nodes/{node_id}/members", status_code=201, summary="Add a member")
def add_member(
    body: MemberIn,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give a person a role on this node. You must hold every right of the role here."""
    _, catalogue = _gate(conn, registry, who, node_id, "nodes.members")
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise ApiError("grants.invalid", 422)
    _person(conn, body.user_id)
    with atomic(conn):
        # The role is read under the write lock: a role edited a moment ago counts.
        role = roles_repo.get_role(conn, body.role_id)
        if role is None:
            raise NotFound()
        may_give(who, catalogue, role["rights"], _scope(node_id), body.expires_at)
        assignment_id = roles_repo.add_assignment(
            conn,
            "user",
            body.user_id,
            body.role_id,
            _scope(node_id),
            body.expires_at,
            who.id,
            commit=False,
        )
    _audit(conn, registry, who, "node.member_add", node_id, user=body.user_id, role=role["name"])
    return {"id": assignment_id}


@router.patch("/nodes/{node_id}/members/{assignment_id}", summary="Change a member's role")
def change_member(
    body: MemberPatch,
    node_id: str = NodeId,
    assignment_id: str = AssignmentId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Change the role or the end date. You must hold the old rights and the new ones."""
    _, catalogue = _gate(conn, registry, who, node_id, "nodes.members")
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise ApiError("grants.invalid", 422)
    scope = _scope(node_id)
    with atomic(conn):
        current = _member_assignment(conn, node_id, assignment_id)
        new_role = roles_repo.get_role(conn, body.role_id or current["role_id"])
        old_role = roles_repo.get_role(conn, current["role_id"])
        if new_role is None or old_role is None:
            raise NotFound()
        old_expiry = grants.parse_expiry(current["expires_at"])
        expiry = None if body.clear_expiry else body.expires_at or old_expiry
        # Taking a role away is as sensitive as giving it: both sides must be within reach.
        may_give(who, catalogue, old_role["rights"], scope, old_expiry)
        may_give(who, catalogue, new_role["rights"], scope, expiry)
        roles_repo.remove_assignment(conn, assignment_id, commit=False)
        new_id = roles_repo.add_assignment(
            conn,
            current["subject_type"],
            current["subject_id"],
            new_role["id"],
            scope,
            expiry,
            who.id,
            commit=False,
        )
    _audit(
        conn,
        registry,
        who,
        "node.member_change",
        node_id,
        subject=current["subject_id"],
        role=new_role["name"],
        was=old_role["name"],
    )
    return {"id": new_id}


@router.delete(
    "/nodes/{node_id}/members/{assignment_id}", status_code=204, summary="Remove a member"
)
def remove_member(
    node_id: str = NodeId,
    assignment_id: str = AssignmentId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Take a role away from a member of this node (you must hold what it gives)."""
    _, catalogue = _gate(conn, registry, who, node_id, "nodes.members")
    with atomic(conn):
        current = _member_assignment(conn, node_id, assignment_id)
        role = roles_repo.get_role(conn, current["role_id"])
        if role is None:
            raise NotFound()
        may_give(
            who,
            catalogue,
            role["rights"],
            _scope(node_id),
            grants.parse_expiry(current["expires_at"]),
        )
        roles_repo.remove_assignment(conn, assignment_id, commit=False)
    registry.access_changed(conn)
    _audit(
        conn,
        registry,
        who,
        "node.member_remove",
        node_id,
        subject=current["subject_id"],
        role=role["name"],
    )


@router.get("/directory/users", summary="Find people to invite")
def directory(
    q: str = Query(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_.@-]+$"),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return up to 20 active people whose name starts with `q`: names only, nothing else.

    For people who manage members of a live node they can use, and who have no right to
    list accounts. At least two letters, and a few searches a minute.
    """
    catalogue = registry.rights()
    managed = {n["id"] for n in nodes_repo.list_nodes(conn)}
    if not any(
        g.scope.startswith("node:")
        and g.scope[5:] in managed
        and who.has("nodes.members", catalogue, g.scope)
        for g in who.grants
    ):
        raise MissingRight()
    if directory_limiter.blocked(user=who.id):
        raise ApiError("members.too_many_searches", 429)
    directory_limiter.remember(user=who.id)
    pattern = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        "SELECT id, username FROM users WHERE username LIKE ? ESCAPE '\\' AND is_active = 1"
        " AND kind = 'person' ORDER BY username LIMIT 20",
        (pattern,),
    )
    return [dict(r) for r in rows]


@router.post("/nodes/{node_id}/transfer", summary="Hand the node over to someone else")
def transfer(
    body: TransferIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Make another person the owner. Only the owner can; you stay on as an administrator.

    It cannot be undone by you, so it asks for what the quick actions of an administrator ask:
    a real session (not a key), the node's name, and a fresh second-factor code.
    """
    node, _ = _gate(conn, registry, who, node_id, "nodes.transfer")
    if who.kind != "session":
        raise MissingRight("members.not_with_key")
    if node["local"]:
        raise ApiError("nodes.local_not_transferable", 409)
    if body.user_id == who.id:
        raise ApiError("nodes.transfer_to_self", 409)
    # The name first: a typo must not burn a one-time code.
    if body.confirm_name != node["name"]:
        raise ApiError("nodeadmin.confirm_mismatch", 422)
    target = _person(conn, body.user_id)
    second_factor.require_fresh_code(conn, request.app.state.settings, who.id, body.code)
    scope = _scope(node_id)
    owner_rights = registry.node_owner_rights()
    with atomic(conn):
        # Everything that decides is read again under the write lock, from the database: two
        # simultaneous hand-overs cannot both succeed.
        fresh = nodes_repo.get_node(conn, node_id)
        now_who = registry.principal_for(conn, who.id)
        if (
            fresh is None
            or fresh["owner"] != who.id
            or not now_who.has("nodes.transfer", registry.rights(), scope)
        ):
            raise MissingRight()
        # The new owner gets clean, permanent owner rights: any older row of theirs for these
        # rights on this node (one that would expire, say) is replaced.
        for g in grants.list_grants(conn, target["id"]):
            if g["scope"] == scope and g["right"] in owner_rights:
                conn.execute("DELETE FROM grants WHERE id = ?", (g["id"],))
        for right in owner_rights:
            grants.add_grant(conn, target["id"], Grant(right, scope), who.id, commit=False)
        for g in grants.list_grants(conn, who.id):
            if g["scope"] == scope and g["right"] in owner_rights:
                conn.execute("DELETE FROM grants WHERE id = ?", (g["id"],))
        # The former owner is not sent away: they stay as an administrator.
        roles_repo.add_assignment(
            conn,
            "user",
            who.id,
            builtin.role_id("administrator"),
            scope,
            None,
            who.id,
            commit=False,
        )
        nodes_repo.set_owner(conn, node_id, target["id"])
    _audit(conn, registry, who, "node.transfer", node_id, to=target["id"])
    return {"owner": target["id"]}
