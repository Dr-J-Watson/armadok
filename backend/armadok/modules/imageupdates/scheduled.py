"""The scheduled action "check for image updates" (every night, for example)."""

from __future__ import annotations

from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import HookAction
from armadok.modules.imageupdates import service
from armadok.modules.workers.access import authorise


def validate(target: dict[str, Any]) -> dict[str, Any]:
    """Accept exactly `{node_id}`."""
    node_id = target.get("node_id")
    if set(target) != {"node_id"} or not isinstance(node_id, str):
        raise ValueError("target must be a node")
    return {"node_id": node_id}


async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
    """Check the node's images, as the creator of the schedule (who still needs the right)."""
    conn = app.state.settings.open_db()
    try:
        authorise(conn, app.state.registry, who, target["node_id"], "imageupdates.check")
    finally:
        conn.close()
    try:
        await service.check_node(app, target["node_id"])
    except service.Busy:
        raise ApiError("imageupdates.busy", 409) from None


ACTION = HookAction("images.check", "imageupdates.check", validate, run, hookable=False)
