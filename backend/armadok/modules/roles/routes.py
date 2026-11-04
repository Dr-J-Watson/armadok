"""Endpoints for roles, groups, assignments and the views of who can do what.

Reading needs `users.read`; changing anything needs `grants.manage` (sensitive), and nobody can
hand out, or take away, more than they hold: every change is checked right by right with the
same rule as direct grants (`can_grant`). A role change takes effect for everyone who has it,
so a right added to a role in use must be one the editor could give wherever the role is given.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from armadok.core import audit, grants
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.roles import builtin
from armadok.modules.roles import repository as repo
from armadok.modules.roles.service import atomic as _atomic
from armadok.modules.roles.service import may_give as _may_give
from armadok.modules.users import repository as users_repo

router = APIRouter(tags=["roles"])

SCOPE_PATTERN = re.compile(r"^(\*|node:[A-Za-z0-9_.-]{1,64})$")
NAME = r"^[^\x00-\x1f\x7f]{1,60}$"
MAX_RIGHTS = 80


class RoleIn(BaseModel):
    """A custom role: a name and the rights it includes."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    rights: list[str] = Field(max_length=MAX_RIGHTS)


class DuplicateIn(BaseModel):
    """Name of the copy of a role."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)


class GroupIn(BaseModel):
    """A group name."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)


class MemberIn(BaseModel):
    """A user to put in a group."""

    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=64)


class AssignmentIn(BaseModel):
    """A role given to a person or a group, on one node or everywhere, until a date (optional)."""

    model_config = ConfigDict(extra="forbid")
    subject_type: str = Field(pattern="^(user|group)$")
    subject_id: str = Field(max_length=64)
    role_id: str = Field(max_length=64)
    scope: str = Field(default="*", max_length=80)
    expires_at: AwareDatetime | None = None


def _audit(conn, registry, who, event, object_type, object_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type=object_type,
        object_id=object_id,
        details=details,
    )


def _visible(who: Principal, catalogue: dict, scope: str) -> bool:
    """Whether a scope may be shown to `who`: everywhere, or a node they can see."""
    return scope == "*" or who.has("nodes.read", catalogue, scope)


def _reserved(name: str) -> bool:
    """Built-in role names (and their translations' keys) cannot be used by custom roles."""
    return name.strip().lower() in builtin.LEVELS_OF


def _manage(who: Principal, registry: Registry) -> dict:
    catalogue = registry.rights()
    who.require("grants.manage", catalogue)
    return catalogue


def _custom_rights(catalogue: dict, rights: list[str]) -> list[str]:
    """Check a custom role's rights: known, given per node, no duplicates."""
    unique = list(dict.fromkeys(rights))
    if any(r not in catalogue or catalogue[r].level is None for r in unique):
        raise ApiError("roles.invalid_rights", 422)
    return unique


def _role(conn: sqlite3.Connection, role_id: str) -> dict:
    role = repo.get_role(conn, role_id)
    if role is None:
        raise NotFound()
    return role


# --- roles -----------------------------------------------------------------------------------


@router.get("/roles", summary="Roles (built-in and custom)")
def list_roles(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return every role with the rights it includes. Needs `users.read`."""
    who.require("users.read", registry.rights())
    return repo.list_roles(conn)


@router.post("/roles", status_code=201, summary="Create a custom role")
def create_role(
    body: RoleIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a role. Needs `grants.manage`.

    A role gives nothing by existing: the check on what you hold happens when it is given.
    """
    catalogue = _manage(who, registry)
    rights = _custom_rights(catalogue, body.rights)
    if _reserved(body.name):
        raise ApiError("roles.name_taken", 409)
    try:
        role_id = repo.create_role(conn, body.name.strip(), rights, who.id)
    except sqlite3.IntegrityError:
        raise ApiError("roles.name_taken", 409) from None
    _audit(conn, registry, who, "role.create", "role", role_id, name=body.name.strip())
    return {"id": role_id}


@router.post("/roles/{role_id}/duplicate", status_code=201, summary="Copy a role")
def duplicate_role(
    role_id: str,
    body: DuplicateIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Copy a role (built-in or custom) under a new name, to adjust it."""
    _manage(who, registry)
    source = _role(conn, role_id)
    if _reserved(body.name):
        raise ApiError("roles.name_taken", 409)
    try:
        new_id = repo.create_role(conn, body.name.strip(), source["rights"], who.id)
    except sqlite3.IntegrityError:
        raise ApiError("roles.name_taken", 409) from None
    _audit(
        conn, registry, who, "role.create", "role", new_id, name=body.name.strip(), copy_of=role_id
    )
    return {"id": new_id}


@router.put("/roles/{role_id}", summary="Change a custom role")
def update_role(
    role_id: str,
    body: RoleIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Rename a custom role and replace its rights.

    A change reaches everyone who has the role: each right added or taken away must be something
    the person changing it could give on every scope where the role is already given.
    """
    catalogue = _manage(who, registry)
    role = _role(conn, role_id)
    if role["builtin"]:
        raise ApiError("roles.builtin_readonly", 409)
    rights = _custom_rights(catalogue, body.rights)
    if _reserved(body.name):
        raise ApiError("roles.name_taken", 409)
    with _atomic(conn):
        role = _role(conn, role_id)
        added = [r for r in rights if r not in role["rights"]]
        removed = [r for r in role["rights"] if r not in rights]
        # The change reaches everyone who has the role, so each right added or taken away must
        # be one the editor could give (or take) on every scope where the role is given.
        for a in repo.list_assignments(conn):
            if a["role_id"] == role_id:
                expiry = grants.parse_expiry(a["expires_at"])
                _may_give(who, catalogue, [*added, *removed], a["scope"], expiry)
        try:
            repo.update_role(conn, role_id, body.name.strip(), rights)
        except sqlite3.IntegrityError:
            raise ApiError("roles.name_taken", 409) from None
    _audit(
        conn,
        registry,
        who,
        "role.update",
        "role",
        role_id,
        name=body.name.strip(),
        added=added,
        removed=removed,
    )
    return {"id": role_id}


@router.delete("/roles/{role_id}", status_code=204, summary="Delete a custom role")
def delete_role(
    role_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete a custom role that nobody uses."""
    _manage(who, registry)
    role = _role(conn, role_id)
    if role["builtin"]:
        raise ApiError("roles.builtin_readonly", 409)
    with _atomic(conn):
        if repo.role_in_use(conn, role_id):
            raise ApiError("roles.in_use", 409)
        repo.delete_role(conn, role_id)
    _audit(conn, registry, who, "role.delete", "role", role_id, name=role["name"])


# --- groups ----------------------------------------------------------------------------------


def _group_assignments(conn: sqlite3.Connection, group_id: str) -> list[dict]:
    return repo.list_assignments(conn, "group", group_id)


def _check_group_rights(conn, who, catalogue, group_id: str) -> None:
    """Check that you hold what the group gives: changing its members or existence moves rights."""
    for a in _group_assignments(conn, group_id):
        role = repo.get_role(conn, a["role_id"])
        if role is None:
            continue
        _may_give(who, catalogue, role["rights"], a["scope"], grants.parse_expiry(a["expires_at"]))


@router.get("/groups", summary="Groups")
def list_groups(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return every group with its members. Needs `users.read`."""
    who.require("users.read", registry.rights())
    return repo.list_groups(conn)


@router.post("/groups", status_code=201, summary="Create a group")
def create_group(
    body: GroupIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create an empty group."""
    _manage(who, registry)
    try:
        group_id = repo.create_group(conn, body.name.strip())
    except sqlite3.IntegrityError:
        raise ApiError("groups.name_taken", 409) from None
    _audit(conn, registry, who, "group.create", "group", group_id, name=body.name.strip())
    return {"id": group_id}


@router.delete("/groups/{group_id}", status_code=204, summary="Delete a group")
def delete_group(
    group_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete a group and the assignments made to it (you must hold what they gave)."""
    catalogue = _manage(who, registry)
    group = repo.get_group(conn, group_id)
    if group is None:
        raise NotFound()
    with _atomic(conn):
        _check_group_rights(conn, who, catalogue, group_id)
        repo.delete_group(conn, group_id)
    _audit(conn, registry, who, "group.delete", "group", group_id, name=group["name"])


@router.post("/groups/{group_id}/members", status_code=204, summary="Add a member to a group")
def add_member(
    group_id: str,
    body: MemberIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Add a person. They receive the roles given to the group, so you must hold those rights."""
    catalogue = _manage(who, registry)
    if repo.get_group(conn, group_id) is None or users_repo.get_user(conn, body.user_id) is None:
        raise NotFound()
    with _atomic(conn):
        _check_group_rights(conn, who, catalogue, group_id)
        repo.add_member(conn, group_id, body.user_id)
    _audit(conn, registry, who, "group.member_add", "group", group_id, user=body.user_id)


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204, summary="Remove a member")
def remove_member(
    group_id: str,
    user_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Take a person out of a group (you must hold the rights they lose)."""
    catalogue = _manage(who, registry)
    if repo.get_group(conn, group_id) is None:
        raise NotFound()
    with _atomic(conn):
        _check_group_rights(conn, who, catalogue, group_id)
        if not repo.remove_member(conn, group_id, user_id):
            raise NotFound()
    _audit(conn, registry, who, "group.member_remove", "group", group_id, user=user_id)


# --- assignments -----------------------------------------------------------------------------


@router.get("/assignments", summary="Role assignments")
def list_assignments(
    subject_type: str | None = None,
    subject_id: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return assignments, optionally of one person or group. Needs `users.read`."""
    catalogue = registry.rights()
    who.require("users.read", catalogue)
    # Assignments on machines the caller cannot see stay out of the answer.
    return [
        a
        for a in repo.list_assignments(conn, subject_type, subject_id)
        if _visible(who, catalogue, a["scope"])
    ]


@router.post("/assignments", status_code=201, summary="Give a role")
def add_assignment(
    body: AssignmentIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give a role to a person or a group on a scope.

    Needs `grants.manage` and every right of the role, for that scope and duration.
    """
    catalogue = _manage(who, registry)
    if not SCOPE_PATTERN.match(body.scope):
        raise ApiError("grants.invalid", 422)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise ApiError("grants.invalid", 422)
    exists = (
        users_repo.get_user(conn, body.subject_id)
        if body.subject_type == "user"
        else repo.get_group(conn, body.subject_id)
    )
    role = repo.get_role(conn, body.role_id)
    if exists is None or role is None:
        raise NotFound()
    # A node the caller cannot see is as "not found" as one that does not exist.
    if body.scope.startswith("node:") and (
        nodes_repo.get_node(conn, body.scope[5:]) is None
        or not who.has("nodes.read", catalogue, body.scope)
    ):
        raise NotFound()
    with _atomic(conn):
        if repo.get_role(conn, body.role_id) is None:
            raise NotFound()
        _may_give(who, catalogue, role["rights"], body.scope, body.expires_at)
        assignment_id = repo.add_assignment(
            conn,
            body.subject_type,
            body.subject_id,
            body.role_id,
            body.scope,
            body.expires_at,
            who.id,
        )
    _audit(
        conn,
        registry,
        who,
        "assignment.add",
        body.subject_type,
        body.subject_id,
        role=role["name"],
        scope=body.scope,
    )
    return {"id": assignment_id}


@router.delete("/assignments/{assignment_id}", status_code=204, summary="Take a role away")
def remove_assignment(
    assignment_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove an assignment (you must hold the rights it gives, as for any removal)."""
    catalogue = _manage(who, registry)
    found = repo.get_assignment(conn, assignment_id)
    role = repo.get_role(conn, found["role_id"]) if found else None
    if found is None or role is None:
        raise NotFound()
    with _atomic(conn):
        _may_give(
            who, catalogue, role["rights"], found["scope"], grants.parse_expiry(found["expires_at"])
        )
        repo.remove_assignment(conn, assignment_id)
    registry.access_changed(conn)
    _audit(
        conn,
        registry,
        who,
        "assignment.remove",
        found["subject_type"],
        found["subject_id"],
        role=role["name"],
        scope=found["scope"],
    )


# --- who can what ----------------------------------------------------------------------------


@router.get("/users/{user_id}/effective", summary="Everything an account can do, and why")
def effective(
    user_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the account's own grants and the rights that come from its roles (with the source)."""
    catalogue = registry.rights()
    who.require("users.read", catalogue)
    if users_repo.get_user(conn, user_id) is None:
        raise NotFound()
    own = [
        {**g, "source": "direct"}
        for g in grants.list_grants(conn, user_id)
        if _visible(who, catalogue, g["scope"])
    ]
    groups = {g["id"]: g["name"] for g in repo.list_groups(conn) if user_id in g["members"]}
    via_roles = []
    for a in repo.list_assignments(conn):
        direct = a["subject_type"] == "user" and a["subject_id"] == user_id
        if not direct and not (a["subject_type"] == "group" and a["subject_id"] in groups):
            continue
        if not _visible(who, catalogue, a["scope"]):
            continue
        role = repo.get_role(conn, a["role_id"])
        for right in role["rights"] if role else []:
            via_roles.append(
                {
                    "right": right,
                    "scope": a["scope"],
                    "expires_at": a["expires_at"],
                    "source": "role",
                    "role": a["role_name"],
                    "group": groups.get(a["subject_id"]) if not direct else None,
                }
            )
    return {"direct": own, "from_roles": via_roles}


@router.get("/nodes/{node_id}/access", summary="Who can do what on a node")
def node_access(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return, for each active account that has any right on the node, the rights that work there.

    Needs `grants.manage` and to see the node. It lists rights, never the node's content.
    """
    catalogue = _manage(who, registry)
    scope = f"node:{node_id}"
    if nodes_repo.get_node(conn, node_id) is None or not who.has("nodes.read", catalogue, scope):
        raise NotFound()
    out = []
    for user in users_repo.list_users(conn):
        if not user["is_active"]:
            continue
        person = Principal(user["id"], grants.load_grants(conn, user["id"]))
        held = sorted(
            r for r, d in catalogue.items() if d.level and person.has(r, catalogue, scope)
        )
        if held:
            out.append({"user_id": user["id"], "username": user["username"], "rights": held})
    return out
