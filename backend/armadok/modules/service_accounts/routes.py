"""Service account endpoints.

A service account is an identity for a tool (CI, monitoring, home automation): no password, no
second factor, no screen login, only API keys. The person who makes it is responsible for it and
is the only one who manages it. Its rights follow that person's rights at each call, unless
the account was created with a fixed list of rights. A key of a service account can never carry
more than both the account and the person creating the key hold, and never a sensitive right.
"""

from __future__ import annotations

import secrets
import sqlite3

from fastapi import APIRouter, Depends, Path
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from armadok.core import audit, grants
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Grant, Principal, can_grant
from armadok.modules.apikeys import repository as keys
from armadok.modules.apikeys import service as key_rules
from armadok.modules.service_accounts import repository as repo
from armadok.modules.users import repository as users_repo
from armadok.modules.users.security import hash_password

router = APIRouter(prefix="/service-accounts", tags=["service-accounts"])

PREFIX = "svc-"
AccountId = Path(pattern=r"^[0-9a-f]{32}$")
KeyId = Path(pattern=r"^[0-9a-f]{12}$")


class GrantIn(BaseModel):
    """A right a fixed-rights account holds, on a scope."""

    model_config = ConfigDict(extra="forbid")
    right: str = Field(max_length=100)
    scope: str = Field(default="*", pattern=r"^(\*|node:[A-Za-z0-9_.-]{1,64})$")


class AccountIn(BaseModel):
    """A new service account."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    description: str = Field(default="", max_length=200)
    # True: the account has the rights of the person who made it, as they are at each call.
    follow_owner: bool = True
    # With follow_owner false: the fixed rights it holds (each one you must hold yourself).
    grants: list[GrantIn] = Field(default_factory=list, max_length=50)


class KeyIn(BaseModel):
    """A key for a service account."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    rights: list[str] = Field(min_length=1, max_length=100)
    read_only: bool = False
    expires_at: AwareDatetime | None = None


def _audit(conn, registry, who, event, account_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="service_account",
        object_id=account_id,
        details=details,
    )


def _manage(who: Principal, registry: Registry) -> dict:
    """Allow people with a real session and the manage right (a key never creates accounts)."""
    if who.kind != "session":
        raise MissingRight("service_accounts.not_with_key")
    catalogue = registry.rights()
    who.require("service_accounts.manage", catalogue)
    return catalogue


def _mine(conn: sqlite3.Connection, who: Principal, account_id: str) -> sqlite3.Row:
    """Return the account if it is a service account this person is responsible for."""
    row = repo.get(conn, account_id)
    if row is None:
        raise NotFound()
    # An account whose responsible person is disabled would otherwise have nobody who can close
    # it: any other holder of the manage right may then take care of it.
    if row["owner_id"] != who.id and not repo.owner_is_inactive(conn, account_id):
        raise NotFound()
    return row


@router.get("", summary="Your service accounts")
def list_accounts(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the service accounts you are responsible for."""
    _manage(who, registry)
    return repo.list_for(conn, who.id)


@router.post("", status_code=201, summary="Create a service account")
def create_account(
    body: AccountIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create the account; you become responsible for it.

    A fixed-rights account gets exactly the rights listed, each of which you must be able to
    give; an account that follows you holds nothing of its own.
    """
    catalogue = _manage(who, registry)
    if body.follow_owner == bool(body.grants):
        raise ApiError("service_accounts.invalid_rights", 422)
    for g in body.grants:
        if g.right not in catalogue or not can_grant(who, Grant(g.right, g.scope), catalogue):
            raise MissingRight()
    username = PREFIX + body.name
    if users_repo.get_user_by_username(conn, username) is not None:
        raise ApiError("service_accounts.name_taken", 409)
    try:
        # A password nobody knows: the account cannot log in (and the login refuses it anyway).
        user = users_repo.create_user(
            conn, username, hash_password(secrets.token_urlsafe(32)), commit=False, kind="service"
        )
        repo.add(conn, user["id"], who.id, body.follow_owner, body.description)
        for g in body.grants:
            grants.add_grant(conn, user["id"], Grant(g.right, g.scope), who.id, commit=False)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError("service_accounts.name_taken", 409) from None
    except BaseException:
        conn.rollback()
        raise
    _audit(
        conn,
        registry,
        who,
        "service_account.create",
        user["id"],
        name=username,
        follows_owner=body.follow_owner,
        grants=[f"{g.right}@{g.scope}" for g in body.grants],
    )
    return {"id": user["id"], "username": username}


@router.delete("/{account_id}", status_code=204, summary="Close a service account")
def close_account(
    account_id: str = AccountId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Disable the account and revoke all its keys at once."""
    _manage(who, registry)
    _mine(conn, who, account_id)
    users_repo.set_active(conn, account_id, False)
    keys.revoke_all(conn, account_id)
    _audit(conn, registry, who, "service_account.close", account_id)


@router.get("/{account_id}/keys", summary="Keys of a service account")
def list_keys(
    account_id: str = AccountId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the account's keys (never their secrets)."""
    _manage(who, registry)
    _mine(conn, who, account_id)
    return keys.list_keys(conn, account_id)


@router.post("/{account_id}/keys", status_code=201, summary="Create a key for the account")
def create_key(
    body: KeyIn,
    account_id: str = AccountId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a key and return it, shown only in this answer.

    Its rights must be held by the account now *and* by you: you cannot lend more than you have.
    """
    catalogue = _manage(who, registry)
    _mine(conn, who, account_id)
    # A key made for a closed account would wake up if the account were ever enabled again.
    account = users_repo.get_user(conn, account_id)
    if account is None or not account["is_active"]:
        raise ApiError("service_accounts.closed", 409)
    account_rights = key_rules.held_rights(registry.grants_of(conn, account_id))
    rights = key_rules.check_rights(
        account_rights & key_rules.held_rights(who.grants), catalogue, body.rights
    )
    key_rules.check_expiry(body.expires_at)
    full_key, record = keys.create_key(
        conn, account_id, body.name, rights, body.read_only, body.expires_at
    )
    _audit(
        conn,
        registry,
        who,
        "service_account.key_create",
        account_id,
        key=record["id"],
        name=body.name,
        rights=rights,
        read_only=body.read_only,
        expires_at=body.expires_at.isoformat() if body.expires_at else None,
    )
    return {**record, "key": full_key}


@router.delete("/{account_id}/keys/{key_id}", status_code=204, summary="Revoke a key")
def revoke_key(
    account_id: str = AccountId,
    key_id: str = KeyId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Revoke one of the account's keys at once."""
    _manage(who, registry)
    _mine(conn, who, account_id)
    if not keys.revoke(conn, account_id, key_id):
        raise NotFound()
    _audit(conn, registry, who, "service_account.key_revoke", account_id, key=key_id)
