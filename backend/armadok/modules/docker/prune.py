"""The scheduled "prune" action: remove stopped containers, dangling images or unused networks."""

from __future__ import annotations

from typing import Any

from armadok.core.module import HookAction
from armadok.modules.workers.access import authorise
from armadok.modules.workers.routes import call_operation

KINDS = ("containers", "images", "networks")


def validate(target: dict[str, Any]) -> dict[str, Any]:
    """Accept exactly `{node_id, kind}` with a known kind."""
    node_id, kind = target.get("node_id"), target.get("kind")
    if set(target) != {"node_id", "kind"} or not isinstance(node_id, str) or kind not in KINDS:
        raise ValueError("target must be a node and a kind")
    return {"node_id": node_id, "kind": kind}


async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
    """Prune, as the creator of the schedule (who must still hold `docker.remove`)."""
    conn = app.state.settings.open_db()
    try:
        authorise(conn, app.state.registry, who, target["node_id"], "docker.remove")
    finally:
        conn.close()
    await call_operation(
        app, app.state.registry, target["node_id"], "docker.prune", target_args(target)
    )


def target_args(target: dict[str, Any]) -> dict[str, Any]:
    """Return the worker arguments for a target."""
    return {"kind": target["kind"]}


# Removing is lossy and runs unattended: only on a schedule, never from a token.
ACTION = HookAction(
    "docker.prune",
    "docker.remove",
    validate,
    run,
    hookable=False,
    fields=(("kind", KINDS),),
)
