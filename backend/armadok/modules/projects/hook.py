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


async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
    """Redeploy: pull the images and bring the project up, as the hook's creator."""
    registry = app.state.registry
    conn = app.state.settings.open_db()
    try:
        authorise(conn, registry, who, target["node_id"], "projects.deploy")
        body = schemas.ActionIn(action="up", pull=True)
        await start_action(app, conn, registry, who, target["node_id"], target["project"], body)
    finally:
        conn.close()


ACTION = HookAction("project.redeploy", "projects.deploy", validate, run)
