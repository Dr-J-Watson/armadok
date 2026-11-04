"""Endpoints of the image scan, per machine.

Reading needs `imagescan.read`; scanning needs `imagescan.run` and the global switch
`ARMADOK_VULN_CHECK`, because a scan sends package names and versions to the OSV database.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.imagescan import repository, service
from armadok.modules.workers.access import authorise

router = APIRouter(prefix="/nodes/{node_id}/imagescan", tags=["imagescan"])
MIN_GAP_SECONDS = 30
_last: dict[str, float] = {}


class ScanIn(BaseModel):
    """One image to scan, or none for every image the machine's containers use."""

    model_config = ConfigDict(extra="forbid")
    image: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


@router.get("", summary="Known flaws in the images of a machine")
def overview(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the latest scan of each image and whether scanning is switched on."""
    authorise(conn, registry, who, node_id, "imagescan.read")
    return {
        "enabled": request.app.state.settings.vuln_check,
        "scans": repository.list_for_node(conn, node_id),
        "can_run": who.has("imagescan.run", registry.rights(), f"node:{node_id}"),
    }


@router.post("/scan", summary="Scan images now")
async def scan(
    node_id: str,
    body: ScanIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Scan one image, or the images in use, and return how each went.

    Package names and versions go to the OSV database; nothing else leaves the machine.
    """
    authorise(conn, registry, who, node_id, "imagescan.run")
    if not request.app.state.settings.vuln_check:
        raise ApiError("vulns.disabled", 409)
    now = time.monotonic()
    if now - _last.get(node_id, -1e9) < MIN_GAP_SECONDS:
        raise ApiError("imagescan.too_soon", 429)
    _last[node_id] = now
    if body.image:
        name = await service.name_of(request.app, node_id, body.image)
        if name is None:
            raise NotFound()
        targets = [(body.image, name)]
    else:
        targets = await service.images_in_use(request.app, node_id)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "imagescan.run",
        object_type="node",
        object_id=node_id,
        details={"images": len(targets)},
    )
    results = []
    for image_id, name in targets:
        try:
            done = await service.scan_image(request.app, node_id, image_id, name)
            results.append({"name": name, **done})
        except ApiError as error:
            results.append({"name": name, "status": error.code, "findings": 0})
    return {"results": results}
