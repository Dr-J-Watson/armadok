"""Following a project's Git branch from code that has no HTTP request (a scheduled task)."""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, ValidationError

from armadok.core import audit, crypto
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.git import repository, schemas
from armadok.modules.projects import states
from armadok.modules.workers.access import audit_failure, authorise
from armadok.modules.workers.routes import call_operation


def login_for(settings: Any, conn: sqlite3.Connection, node_id: str, url: str) -> dict | None:
    """Return the stored login for the address's host as the worker takes it, or None."""
    sealed = repository.sealed_login(conn, node_id, schemas.host_of(url))
    if sealed is None:
        return None
    return {"username": sealed[0], "token": crypto.decrypt(settings, sealed[1])}


async def _ask(app: Any, node_id: str, op: str, args: dict, model: type[BaseModel]) -> Any:
    """Run a Git operation on the node and validate the (untrusted) answer."""
    result = await call_operation(app, app.state.registry, node_id, op, args)
    try:
        return model.model_validate(result)
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None


async def pull_if_behind(
    app: Any,
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    node_id: str,
    project: str,
) -> bool:
    """Bring a Git-sourced project to its branch's newest commit; say whether it moved.

    Returns False for a project with no Git source or already up to date. Needs
    `projects.files.write` on the project (the caller holds `projects.deploy`, which does not
    imply it). A refusal by the node (files edited by hand, repository unreachable) raises
    `ApiError` with its code and is audited as a failure.
    """
    saved = repository.get_source(conn, node_id, project)
    if saved is None:
        return False
    authorise(conn, registry, who, node_id, "projects.files.write", ("project", project))
    states.require_managed(conn, node_id, project)
    args = {
        "project": project,
        "url": saved["url"],
        "branch": saved["branch"],
        "login": login_for(app.state.settings, conn, node_id, saved["url"]),
    }
    status = await _ask(app, node_id, "git.status", args, schemas.Status)
    if status.remote == status.commit:
        return False
    details = {"project": project, "url": saved["url"], "branch": saved["branch"]}
    try:
        answer = await _ask(app, node_id, "git.pull", args, schemas.Commit)
    except ApiError as error:
        audit_failure(conn, registry, who, "git.update", node_id, details, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "git.update",
        object_type="node",
        object_id=node_id,
        details={**details, "commit": answer.commit[:12]},
    )
    return True
