"""The scheduled action "scan the images in use" (daily, for example, from the schedule tab)."""

from __future__ import annotations

from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import HookAction
from armadok.modules.imagescan import service
from armadok.modules.workers.access import authorise


def validate(target: dict[str, Any]) -> dict[str, Any]:
    """Accept exactly `{node_id}`."""
    node_id = target.get("node_id")
    if set(target) != {"node_id"} or not isinstance(node_id, str):
        raise ValueError("target must be a node")
    return {"node_id": node_id}


async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
    """Scan the images the node's containers use, as the creator of the schedule."""
    if not app.state.settings.vuln_check:
        raise ApiError("vulns.disabled", 409)
    conn = app.state.settings.open_db()
    try:
        authorise(conn, app.state.registry, who, target["node_id"], "imagescan.run")
    finally:
        conn.close()
    for image_id, name in await service.images_in_use(app, target["node_id"]):
        try:
            await service.scan_image(app, target["node_id"], image_id, name)
        except ApiError as error:
            if error.code != "imagescan.busy":
                raise


ACTION = HookAction("imagescan.scan", "imagescan.run", validate, run, hookable=False)
