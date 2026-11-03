"""Deployment endpoints of a project: run an action, follow it, see status and logs.

Running an action is as powerful as running `docker compose` on the machine, so it needs the
sensitive `projects.deploy` right (which API keys cannot carry). Every start and every end is
audited, and people are notified of success or failure.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.events import emit
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.projects import schemas, tasks
from armadok.modules.workers.access import ask, audit_failure, authorise
from armadok.modules.workers.routes import call_operation

_watchers: set[asyncio.Task[None]] = set()  # strong references to running watchers

router = APIRouter(prefix="/nodes/{node_id}", tags=["projects"])
log = logging.getLogger("armadok.deploy")

POLL_SECONDS = 2
WATCH_FOR_SECONDS = 16 * 60
_PROJECT = re.compile(schemas.PROJECT_PATTERN)
Project = Path(pattern=schemas.PROJECT_PATTERN)


def _args(project: str, body: schemas.ActionIn | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"project": project}
    if body is not None:
        args.update(action=body.action, files=body.files, service=body.service)
        if body.action == "up":
            args.update(build=body.build, pull=body.pull)
    return args


async def sync_task(app: Any, node_id: str, row: sqlite3.Row) -> sqlite3.Row:
    """Ask the worker how a running task is doing, and record its end once."""
    if row["state"] != "running":
        return row
    settings = app.state.settings
    registry = app.state.registry
    try:
        result = await ask_app(app, registry, node_id, "compose.task", {"task": row["worker_task"]})
    except ApiError as error:
        if error.code != "worker.not_found":
            return row  # the worker is away: try again later
        result = schemas.TaskState(
            project=row["project"],
            action=row["action"],
            state="failed",
            exit_code=None,
            output="The worker no longer knows this task.",
        )
    if result.state == "running":
        return row
    conn = settings.open_db()
    try:
        moved = tasks.finish(
            conn,
            settings,
            row["id"],
            result.state,
            result.exit_code,
            result.output,
            result.truncated,
        )
        if moved:
            ok = result.state == "success"
            audit.record(
                conn,
                registry.audit_events(),
                row["started_by"],
                "project.action_done",
                object_type="node",
                object_id=node_id,
                outcome="success" if ok else "failure",
                details={
                    "project": row["project"],
                    "action": row["action"],
                    "exit": result.exit_code,
                },
            )
        if moved and tasks.claim_notification(conn, row["id"]):
            name = (nodes_repo.get_node(conn, node_id) or {}).get("name", node_id)
            await emit(
                app,
                "notify",
                event="deploy.succeeded" if ok else "deploy.failed",
                node_id=node_id,
                params={"project": row["project"], "action": row["action"], "node": name},
            )
        return tasks.get(conn, node_id, row["id"])
    finally:
        conn.close()


async def ask_app(app: Any, registry: Registry, node_id: str, op: str, args: dict) -> Any:
    """Run a worker operation and validate its answer (for code that has no request)."""
    models = {
        "compose.task": schemas.TaskState,
        "compose.start": schemas.Started,
    }
    result = await call_operation(app, registry, node_id, op, args)
    try:
        return models[op].model_validate(result)
    except ValueError:
        raise ApiError("worker.invalid_answer", 502) from None


async def _watch(app: Any, node_id: str, task_id: str) -> None:
    """Follow a task until it ends, so its end is recorded and announced without anyone asking."""
    deadline = asyncio.get_running_loop().time() + WATCH_FOR_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        conn = app.state.settings.open_db()
        try:
            row = tasks.get(conn, node_id, task_id)
        finally:
            conn.close()
        if row is None or row["state"] != "running":
            return
        try:
            await sync_task(app, node_id, row)
        except Exception:  # noqa: BLE001  (keep watching whatever one poll does)
            log.exception("could not follow a deployment task")


@router.post("/projects/{project}/actions", status_code=202, summary="Run an action on a project")
async def run_action(
    node_id: str,
    body: schemas.ActionIn,
    request: Request,
    project: str = Project,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Start `up`, `down`, `stop`, `start`, `restart`, `pull` or `build`; returns a task id.

    Needs `projects.deploy` on the node. The action runs in the background: follow it with
    `GET /nodes/{id}/tasks/{task_id}`.
    """
    authorise(conn, registry, who, node_id, "projects.deploy")
    details = {
        "project": project,
        "action": body.action,
        "service": body.service,
        "build": body.build,
        "pull": body.pull,
    }
    try:
        started = await ask(
            request, registry, node_id, "compose.start", _args(project, body), schemas.Started
        )
    except ApiError as error:
        audit_failure(conn, registry, who, "project.action", node_id, details, error)
        raise
    task_id = tasks.create(conn, node_id, started.task, project, body.action, who.id)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "project.action",
        object_type="node",
        object_id=node_id,
        details={**details, "task": task_id},
    )
    watcher = asyncio.create_task(_watch(request.app, node_id, task_id))
    _watchers.add(watcher)  # a task nobody references can be collected before it ends
    watcher.add_done_callback(_watchers.discard)
    return {"task_id": task_id, "state": "running"}


@router.get("/tasks/{task_id}", summary="State and output of a deployment task")
async def get_task(
    node_id: str,
    task_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return a task of this node, refreshed while it runs. Needs `projects.logs`."""
    authorise(conn, registry, who, node_id, "projects.logs")
    row = tasks.get(conn, node_id, task_id)
    if row is None:
        raise NotFound()
    row = await sync_task(request.app, node_id, row)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "project.logs_read",
        object_type="node",
        object_id=node_id,
        details={"project": row["project"], "task": task_id},
    )
    return tasks.view(request.app.state.settings, row)


@router.get("/tasks", summary="Recent deployment tasks of a node")
def list_tasks(
    node_id: str,
    request: Request,
    project: str | None = Query(default=None, pattern=schemas.PROJECT_PATTERN),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the latest tasks (without their output). Needs `projects.logs`."""
    authorise(conn, registry, who, node_id, "projects.logs")
    shown = []
    for row in tasks.recent(conn, node_id, project):
        item = tasks.view(request.app.state.settings, row)
        item.pop("output")
        shown.append(item)
    return shown


@router.get("/projects/{project}/status", summary="State of a project's containers")
async def project_status(
    node_id: str,
    request: Request,
    project: str = Project,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return each container of the project with its state. Needs `projects.read`."""
    authorise(conn, registry, who, node_id, "projects.read")
    answer = await ask(
        request, registry, node_id, "compose.ps", _args(project), schemas.ProjectStatus
    )
    return answer.model_dump()


@router.get("/projects/{project}/logs", summary="Recent logs of a project")
async def project_logs(
    node_id: str,
    request: Request,
    project: str = Project,
    service: str | None = Query(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
    tail: int = Query(default=200, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the last log lines. Needs `projects.logs`. Reads are audited."""
    authorise(conn, registry, who, node_id, "projects.logs")
    args = {**_args(project), "service": service, "tail": tail}
    try:
        answer = await ask(request, registry, node_id, "compose.logs", args, schemas.ProjectLogs)
    except ApiError as error:
        audit_failure(
            conn, registry, who, "project.logs_read", node_id, {"project": project}, error
        )
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "project.logs_read",
        object_type="node",
        object_id=node_id,
        details={"project": project, "service": service, "tail": tail},
    )
    return answer.model_dump()


@router.post("/projects/{project}/check", summary="Check a project's Compose file")
async def check_project(
    node_id: str,
    request: Request,
    project: str = Project,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Check the Compose file is valid, changing nothing. Needs `projects.logs`.

    Compose error messages can quote file content, so this is as sensitive as logs.
    """
    authorise(conn, registry, who, node_id, "projects.logs")
    answer = await ask(
        request, registry, node_id, "compose.check", _args(project), schemas.ComposeCheck
    )
    return answer.model_dump()
