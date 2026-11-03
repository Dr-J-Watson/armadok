"""User administration and rights attribution endpoints."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, Field

from armadok.core import audit, grants
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Grant, Principal, can_grant
from armadok.modules.users import repository
from armadok.modules.users.routes_auth import USERNAME_PATTERN
from armadok.modules.users.security import MIN_PASSWORD_LENGTH, hash_password

router = APIRouter(prefix="/users", tags=["users"])
# Not under /users: the catalogue is not about one account.
catalogue_router = APIRouter(tags=["users"])

SCOPE_PATTERN = re.compile(r"^(\*|[a-z_]+:[A-Za-z0-9_.-]+)$")


class UserIn(BaseModel):
    """Fields to create an account."""

    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class UserPatch(BaseModel):
    """Fields to change on an account; omitted fields are left unchanged."""

    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=256)


class GrantIn(BaseModel):
    """A right to give: `scope` is `*` or `<kind>:<id>`, `expires_at` optional."""

    right: str = Field(max_length=100)
    scope: str = Field(default="*", max_length=200)
    # Timezone-aware only: a naive date would make later comparisons fail.
    expires_at: AwareDatetime | None = None


def _audit(conn, registry, who, event, user_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="user",
        object_id=user_id,
        details=details,
    )


def _existing(conn: sqlite3.Connection, user_id: str) -> dict:
    user = repository.get_user(conn, user_id)
    if user is None:
        raise NotFound()
    return user


def _outranks(conn: sqlite3.Connection, who: Principal, user_id: str, registry: Registry) -> bool:
    """Return True if every active grant of the target is covered by `who`'s own rights.

    The target's grants include those it borrows (a service account follows its person).
    """
    catalogue = registry.rights()
    now = datetime.now(UTC)
    for g in registry.grants_of(conn, user_id):
        if g.expires_at is not None and g.expires_at <= now:
            continue
        if not can_grant(who, g, catalogue):
            return False
    return True


@catalogue_router.get("/rights", summary="The catalogue of rights")
def rights_catalogue(
    registry: Registry = Depends(get_registry), who: Principal = Depends(get_principal)
) -> list[dict]:
    """Return every right with its dependencies and flags, so the interface can offer them.

    Any logged-in account may read it: knowing that a right exists gives no access to anything.
    """
    return [
        {
            "id": r.id,
            "depends_on": list(r.depends_on),
            "sensitive": r.sensitive,
            "node_owner": r.node_owner,
        }
        for r in sorted(registry.rights().values(), key=lambda r: r.id)
    ]


@router.get("", summary="List accounts")
def list_users(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return all accounts. Requires `users.read`."""
    who.require("users.read", registry.rights())
    return repository.list_users(conn)


@router.post("", status_code=201, summary="Create an account")
def create_user(
    body: UserIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create an account with no rights. Requires `users.create`."""
    who.require("users.create", registry.rights())
    # The prefix belongs to service accounts, so a person cannot take a name one will need.
    if body.username.lower().startswith("svc-"):
        raise ApiError("users.username_taken", 409)
    try:
        user = repository.create_user(conn, body.username, hash_password(body.password))
    except sqlite3.IntegrityError:
        raise ApiError("users.username_taken", 409) from None
    for right in registry.baseline_rights():
        grants.add_grant(conn, user["id"], Grant(right), granted_by=who.id)
    _audit(conn, registry, who, "user.create", user["id"])
    return user


@router.patch("/{user_id}", summary="Enable, disable or reset the password of an account")
def update_user(
    user_id: str,
    body: UserPatch,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Change an account. Requires `users.update`. Disabling or a new password ends its sessions."""
    catalogue = registry.rights()
    who.require("users.update", catalogue)
    _existing(conn, user_id)
    target = _existing(conn, user_id)
    if target["kind"] != "person" and body.password is not None:
        raise ApiError("users.not_a_person", 409)  # a service account has no password
    if body.password is not None:
        who.require("users.reset_password", catalogue)
    if body.is_active is False and user_id == who.id:
        raise ApiError("users.cannot_disable_self", 409)
    # Acting on someone else's account must not be a way around "no more than you hold":
    # you may only touch accounts whose every right you could have granted yourself.
    if user_id != who.id and not _outranks(conn, who, user_id, registry):
        raise MissingRight()
    if body.is_active is not None:
        repository.set_active(conn, user_id, body.is_active)
    if body.password is not None:
        repository.set_password(conn, user_id, hash_password(body.password))
    # Only the names of changed fields are logged, never a password.
    changed = [k for k, v in body.model_dump().items() if v is not None]
    _audit(conn, registry, who, "user.update", user_id, fields=",".join(changed))
    return _existing(conn, user_id)


@router.delete("/{user_id}/2fa", status_code=204, summary="Reset the 2FA of an account")
def reset_2fa(
    user_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove the second factor of an account whose owner lost their device.

    Requires `users.reset_2fa`, and only for accounts whose rights the caller could have
    granted. The account's sessions are ended; the action is audited.
    """
    catalogue = registry.rights()
    who.require("users.reset_2fa", catalogue)
    if _existing(conn, user_id)["kind"] != "person":
        raise ApiError("users.not_a_person", 409)
    if user_id != who.id and not _outranks(conn, who, user_id, registry):
        raise MissingRight()
    repository.disable_totp(conn, user_id)
    repository.revoke_other_sessions(conn, user_id, None)
    _audit(conn, registry, who, "user.2fa_reset", user_id)


@router.get("/{user_id}/grants", summary="List the rights of an account")
def list_grants(
    user_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the stored grants. Requires `users.read`."""
    who.require("users.read", registry.rights())
    _existing(conn, user_id)
    return grants.list_grants(conn, user_id)


@router.post("/{user_id}/grants", status_code=201, summary="Give a right")
def add_grant(
    user_id: str,
    body: GrantIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, str]:
    """Give a right to an account.

    Requires `grants.manage` and holding the right oneself for that scope and duration:
    nobody can give more than they have.
    """
    catalogue = registry.rights()
    who.require("grants.manage", catalogue)
    _existing(conn, user_id)
    if body.right not in catalogue or not SCOPE_PATTERN.match(body.scope):
        raise ApiError("grants.invalid", 422)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise ApiError("grants.invalid", 422)
    grant = Grant(body.right, body.scope, body.expires_at)
    if not can_grant(who, grant, catalogue):
        raise MissingRight()
    grant_id = grants.add_grant(conn, user_id, grant, granted_by=who.id)
    _audit(conn, registry, who, "grant.add", user_id, right=body.right, scope=body.scope)
    return {"id": grant_id}


@router.delete("/{user_id}/grants/{grant_id}", status_code=204, summary="Remove a right")
def remove_grant(
    user_id: str,
    grant_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove a right. Requires `grants.manage` and holding that right oneself."""
    catalogue = registry.rights()
    who.require("grants.manage", catalogue)
    _existing(conn, user_id)
    stored = next((g for g in grants.list_grants(conn, user_id) if g["id"] == grant_id), None)
    if stored is None:
        raise NotFound()
    held = Grant(stored["right"], stored["scope"], grants.parse_expiry(stored["expires_at"]))
    if not can_grant(who, held, catalogue):
        raise MissingRight()
    grants.remove_grant(conn, user_id, grant_id)
    _audit(
        conn, registry, who, "grant.remove", user_id, right=stored["right"], scope=stored["scope"]
    )
