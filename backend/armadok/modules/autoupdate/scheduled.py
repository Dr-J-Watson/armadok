"""The scheduled action "update a project": pull its Git branch, then redeploy if anything is new.

Meant to run on a schedule (nightly, say). It does nothing, quietly, when nothing changed, so a
frequent schedule costs nothing. What counts as new: the project's Git branch has a newer
commit, or one of the images its running containers use has a newer version in its registry.
Only a project that is already running is redeployed: updating never starts a stopped one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import HookAction
from armadok.modules.autoupdate import repository
from armadok.modules.docker import schemas as docker_schemas
from armadok.modules.git import repository as git_repo
from armadok.modules.git import service as git_service
from armadok.modules.imageupdates import service as image_service
from armadok.modules.projects import schemas, states, tasks
from armadok.modules.projects.deploy import start_action
from armadok.modules.schedule import repository as schedule_repo
from armadok.modules.workers.access import authorise
from armadok.modules.workers.routes import call_operation

_PROJECT = re.compile(schemas.PROJECT_PATTERN)
_NODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
COMPOSE_LABEL = "com.docker.compose.project"
ACTION_NAME = "project.autoupdate"


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


async def _running_images(app: Any, node_id: str, project: str) -> list[str]:
    """Return the image references of the project's running containers (none: not running)."""
    raw = await call_operation(app, app.state.registry, node_id, "docker.containers.list", {})
    try:
        containers = docker_schemas.ContainerList.model_validate(raw).containers
    except ValueError:
        raise ApiError("worker.invalid_answer", 502) from None
    return [
        c.image
        for c in containers
        if c.state == "running" and c.labels.get(COMPOSE_LABEL) == project
    ]


def check(conn: Any, registry: Any, who: Any, target: dict[str, Any]) -> None:
    """At creation: the creator must see the project and hold, on it, every right a run needs.

    A run needs `projects.deploy`, and `projects.files.write` too when the project follows Git.
    A project can have one automatic update only (two would overlap).
    """
    node_id, project = target["node_id"], target["project"]
    authorise(conn, registry, who, node_id, "projects.deploy", ("project", project))
    states.require_managed(conn, node_id, project)
    if git_repo.get_source(conn, node_id, project) is not None:
        authorise(conn, registry, who, node_id, "projects.files.write", ("project", project))
        states.require_managed(conn, node_id, project)
    for row in schedule_repo.list_for_node(conn, node_id):
        if row["action"] == ACTION_NAME and json.loads(row["target"]).get("project") == project:
            raise ApiError("autoupdate.already_scheduled", 409)


async def run(app: Any, who: Any, target: dict[str, Any]) -> None:
    """Update the project as the creator of the schedule (who still needs the rights).

    Order matters: the branch is pulled first (a refused pull, such as files edited by hand,
    stops everything and is reported as the task's failure). A pulled update is written down
    at once ("owed"), so a run cut short afterwards (timeout, restart) still redeploys it next
    time. The project is then redeployed with its images pulled, and rebuilt for new files.
    A redeploy brings the whole project up, as `docker compose up` does.
    """
    registry = app.state.registry
    node_id, project = target["node_id"], target["project"]
    conn = app.state.settings.open_db()
    try:
        authorise(conn, registry, who, node_id, "projects.deploy", ("project", project))
        states.require_managed(conn, node_id, project)
        if any(r["state"] == "running" for r in tasks.recent(conn, node_id, project)):
            return  # a deployment is under way: this run would pile onto it
        images = await _running_images(app, node_id, project)
        if await git_service.pull_if_behind(app, conn, registry, who, node_id, project):
            repository.set_pending(conn, node_id, project, True)
        owed = repository.is_pending(conn, node_id, project)
        if not images:
            if owed:  # stopped: the files are fresh, and nothing is started behind anyone's back
                repository.set_pending(conn, node_id, project, False)
            return
        by_images = (
            not owed
            and repository.image_cooldown_over(conn, node_id, project)
            and await image_service.newer_exists(app, node_id, images)
        )
        if not (owed or by_images):
            return
        # The project may have been stopped while the checks ran: look again before starting.
        if not await _running_images(app, node_id, project):
            repository.set_pending(conn, node_id, project, False)
            return
        body = schemas.ActionIn(action="up", pull=True, build=owed)
        await start_action(app, conn, registry, who, node_id, project, body)
        repository.mark_redeployed(conn, node_id, project, by_images)
    finally:
        conn.close()


ACTION = HookAction(
    ACTION_NAME,
    "projects.deploy",
    validate,
    run,
    hookable=False,
    fields=(("project", ()),),
    check=check,
)
