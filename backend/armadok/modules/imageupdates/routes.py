"""Endpoints of image update tracking, per machine.

Reading needs `imageupdates.read`; checking needs `imageupdates.check`. A check makes the hub
ask the registries named by the images the machine runs (a `HEAD` each, anonymous unless the
machine has a login stored for that registry): only image
names and tags leave Armadok, and the answers are treated as untrusted.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit, crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.imageupdates import refs, repository, service
from armadok.modules.workers.access import authorise

router = APIRouter(prefix="/nodes/{node_id}/image-updates", tags=["imageupdates"])
MIN_GAP_SECONDS = 60
_last: dict[str, float] = {}


@router.get("", summary="Newer versions of the images a machine runs")
def overview(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the latest comparison of each image in use."""
    authorise(conn, registry, who, node_id, "imageupdates.read")
    return {
        "images": repository.list_for_node(conn, node_id),
        "can_check": who.has("imageupdates.check", registry.rights(), f"node:{node_id}"),
    }


@router.post("/check", summary="Check for newer versions now")
async def check(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Compare the images in use with their registries and return the counts by state."""
    authorise(conn, registry, who, node_id, "imageupdates.check")
    now = time.monotonic()
    if now - _last.get(node_id, -1e9) < MIN_GAP_SECONDS:
        raise ApiError("imageupdates.too_soon", 429)
    _last[node_id] = now
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "imageupdates.check",
        object_type="node",
        object_id=node_id,
    )
    try:
        counts = await service.check_node(request.app, node_id)
    except service.Busy:
        _last.pop(node_id, None)  # nothing ran: the slot is not used up
        raise ApiError("imageupdates.busy", 409) from None
    return {"counts": counts}


class LoginIn(BaseModel):
    """A login for one registry: the host, and the user and password (or access token)."""

    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$")
    secret: str = Field(min_length=1, max_length=2000, pattern=r"^[\x21-\x7e]+$")


@router.get("/registries", summary="Registries this machine has a login for")
def list_logins(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return each registry host and user name. The secret is never returned."""
    authorise(conn, registry, who, node_id, "imageupdates.credentials")
    return {"registries": repository.list_logins(conn, node_id)}


@router.put("/registries", status_code=204, summary="Save the login for a registry")
def save_login(
    node_id: str,
    body: LoginIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Keep (encrypted) the login used to ask a private registry about this machine's images.

    It is used only for the version checks and is sent only to that registry and its own token
    service. Replaces an earlier login for the same host. The audit entry names the host and
    the user, never the secret.
    """
    authorise(conn, registry, who, node_id, "imageupdates.credentials")
    host = refs.login_host(body.host)
    if host is None:
        raise ApiError("imageupdates.invalid_host", 422)
    sealed = crypto.encrypt(request.app.state.settings, body.secret)
    if not repository.save_login(conn, node_id, host, body.username, sealed, who.actor):
        raise ApiError("imageupdates.too_many_logins", 409)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "imageupdates.login_save",
        object_type="node",
        object_id=node_id,
        details={"host": host, "username": body.username[:40]},
    )


@router.delete("/registries/{host}", status_code=204, summary="Forget the login for a registry")
def delete_login(
    node_id: str,
    host: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove the stored login of one registry."""
    authorise(conn, registry, who, node_id, "imageupdates.credentials")
    name = refs.login_host(host)
    if name is None or not repository.delete_login(conn, node_id, name):
        raise ApiError("imageupdates.no_such_login", 404)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "imageupdates.login_delete",
        object_type="node",
        object_id=node_id,
        details={"host": name},
    )
