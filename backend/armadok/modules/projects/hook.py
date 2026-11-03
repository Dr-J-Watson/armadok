"""The incoming-hook action of projects: redeploy one project (pull images, then `up`)."""

from __future__ import annotations

import re
from typing import Any

from armadok.core.module import HookAction
from armadok.modules.projects import schemas
from armadok.modules.projects.deploy import start_action
from armadok.modules.workers.access import authorise

_PROJECT = re.compile(schemas.PROJECT_PATTERN)
_NODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def validate(target: dict[str, Any]) -> dict[str, Any]:
    """Accept exactly `{node_id, project}` with plain names."""
    node_id, project = target.get("node_id"), target.get("project")
    if (
        set(target) != {"node_id", "project"}
        or not isinstance(node_id, str)
        or not _NODE.fullmatch(node_id)
        or not isinstance(project, str)
        or not _PROJECT.fullmatch(project)
    ):
        raise ValueError("target must be a node and a project")
    return {"node_id": node_id, "project": project}


def _runner(body: schemas.ActionIn):
    """Build the function that starts `body` on the project named by a target, as `who`."""

    async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
        registry = app.state.registry
        conn = app.state.settings.open_db()
        try:
            authorise(conn, registry, who, target["node_id"], "projects.deploy")
            await start_action(app, conn, registry, who, target["node_id"], target["project"], body)
        finally:
            conn.close()

    return run


_FIELDS = (("project", ()),)
# Redeploy: pull the images and bring the project up, as the creator of the hook or schedule.
ACTION = HookAction(
    "project.redeploy",
    "projects.deploy",
    validate,
    _runner(schemas.ActionIn(action="up", pull=True)),
    fields=_FIELDS,
)
# A scheduled restart: no reason to let a token trigger it, so not for hooks.
RESTART = HookAction(
    "project.restart",
    "projects.deploy",
    validate,
    _runner(schemas.ActionIn(action="restart")),
    hookable=False,
    fields=_FIELDS,
)
