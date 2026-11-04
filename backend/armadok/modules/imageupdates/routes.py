"""Endpoints of image update tracking, per machine.

Reading needs `imageupdates.read`; checking needs `imageupdates.check`. A check makes the hub
ask the registries named by the images the machine runs (a `HEAD` each, anonymous): only image
names and tags leave Armadok, and the answers are treated as untrusted.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.imageupdates import repository, service
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
