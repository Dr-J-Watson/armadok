"""The scheduled "back up a volume" action (no hook may trigger it)."""

from __future__ import annotations

from typing import Any

from armadok.core.module import HookAction
from armadok.modules.backups import service
from armadok.modules.workers.access import authorise


def validate(target: dict[str, Any]) -> dict[str, Any]:
    """Accept `{node_id, volume}` and an optional `keep` (how many archives to keep)."""
    node_id, volume, keep = target.get("node_id"), target.get("volume"), target.get("keep")
    if (
        not set(target) <= {"node_id", "volume", "keep"}
        or not isinstance(node_id, str)
        or not isinstance(volume, str)
        or not service.VOLUME.fullmatch(volume)
    ):
        raise ValueError("target must be a node and a volume")
    out: dict[str, Any] = {"node_id": node_id, "volume": volume}
    if keep is not None:
        # Targets travel as strings in the API forms; the number is checked either way.
        try:
            number = int(keep)
        except (TypeError, ValueError):
            raise ValueError("keep must be a number") from None
        if not 1 <= number <= 365:
            raise ValueError("keep out of range")
        out["keep"] = number
    return out


async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
    """Back up, as the creator of the schedule (who must still hold `backups.create`)."""
    keep = target.get("keep", service.DEFAULT_KEEP)
    conn = app.state.settings.open_db()
    try:
        authorise(conn, app.state.registry, who, target["node_id"], "backups.create")
        if keep != service.DEFAULT_KEEP:  # rotation deletes archives: see the backup route
            who.require("backups.manage", app.state.registry.rights(), f"node:{target['node_id']}")
    finally:
        conn.close()
    await service.start(
        app, target["node_id"], who.id, "backup", target["volume"], keep=keep, actor=who.actor
    )


ACTION = HookAction(
    "volume.backup",
    "backups.create",
    validate,
    run,
    hookable=False,
    fields=(("volume", ()),),
)
