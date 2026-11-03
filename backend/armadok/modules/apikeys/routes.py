"""API key endpoints: create, list and revoke one's own keys.

A key can never manage keys: these routes refuse to be called with a key, so a leaked
key cannot create more keys or outlive its own revocation.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.apikeys import repository

router = APIRouter(prefix="/apikeys", tags=["apikeys"])


class KeyIn(BaseModel):
    """What a key may do. `rights` is a subset of the creator's own rights."""

    name: str = Field(min_length=1, max_length=100)
    rights: list[str] = Field(min_length=1, max_length=100)
    read_only: bool = False
    expires_at: AwareDatetime | None = None


def _own_keys_only(who: Principal, registry: Registry) -> None:
    """Require the baseline right and a real session (not a key)."""
    if who.kind != "session":
        raise MissingRight("apikeys.not_with_key")
    who.require("apikeys.own", registry.rights())


@router.get("", summary="List your API keys")
def list_keys(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the caller's keys (never the secrets)."""
    _own_keys_only(who, registry)
    return repository.list_keys(conn, who.id)


@router.post("", status_code=201, summary="Create an API key")
def create_key(
    body: KeyIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a key and return it. The key is shown only in this response.

    Each requested right must be one the caller holds now, must not be sensitive, and its
    dependencies must be requested too (a right without them would silently not work).
    """
    _own_keys_only(who, registry)
    catalogue = registry.rights()
    now = datetime.now(UTC)
    held = {g.right for g in who.grants if g.expires_at is None or g.expires_at > now}
    requested = set(body.rights)
    for right in requested:
        if right not in catalogue or catalogue[right].sensitive or right not in held:
            raise ApiError("apikeys.invalid_right", 422)
        if not set(catalogue[right].depends_on) <= requested:
            raise ApiError("apikeys.dependency_missing", 422)
    if body.expires_at is not None and body.expires_at <= now:
        raise ApiError("apikeys.invalid_expiry", 422)
    full_key, record = repository.create_key(
        conn, who.id, body.name, sorted(requested), body.read_only, body.expires_at
    )
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "apikey.create",
        object_type="apikey",
        object_id=record["id"],
        details={"name": body.name, "read_only": body.read_only},
    )
    return {**record, "key": full_key}


@router.delete("/{key_id}", status_code=204, summary="Revoke an API key")
def revoke_key(
    key_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Revoke one of the caller's keys immediately."""
    _own_keys_only(who, registry)
    if not repository.revoke(conn, who.id, key_id):
        raise NotFound()
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "apikey.revoke",
        object_type="apikey",
        object_id=key_id,
    )
