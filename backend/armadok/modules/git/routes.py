"""Endpoints of Git sources: logins for private repositories, cloning, checking, updating.

Cloning makes a new project from a repository's branch; updating brings it to the branch's
newest commit. Neither deploys anything: that stays a separate action with its own right. A
login is stored encrypted, never returned, and travels to the node only inside the one
command that needs it, scoped to the repository's own address.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from armadok.core import audit, crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.git import repository, schemas, service
from armadok.modules.projects import schemas as project_schemas
from armadok.modules.projects import states
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/git", tags=["git"])
_PROJECT = re.compile(project_schemas.PROJECT_PATTERN)


class SourceIn(BaseModel):
    """A repository address (https) and a branch."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(pattern=schemas.URL_PATTERN, max_length=500)
    branch: str = Field(default="main", pattern=schemas.BRANCH_PATTERN, max_length=101)

    @field_validator("branch")
    @classmethod
    def plain_branch(cls, value: str) -> str:
        """Refuse names Git itself would not allow (and that could look like an option)."""
        if ".." in value or "//" in value or value.endswith((".lock", "/")):
            raise ValueError("not a plain branch name")
        return value


class CloneIn(SourceIn):
    """A new project made from a repository."""

    name: str = Field(pattern=project_schemas.PROJECT_PATTERN)


class LoginIn(BaseModel):
    """A login for one Git host: user name and access token."""

    model_config = ConfigDict(extra="forbid")
    host: str = Field(pattern=schemas.HOST_PATTERN, max_length=260)
    username: str = Field(min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$")
    secret: str = Field(min_length=1, max_length=2000, pattern=r"^[\x21-\x7e]+$")


def _project(name: str) -> None:
    """Refuse a malformed project name before it goes anywhere near a worker."""
    if not _PROJECT.fullmatch(name):
        raise ApiError("projects.invalid_name", 422)


def _login_for(request: Request, conn: sqlite3.Connection, node_id: str, url: str):
    """Return the stored login for the address's host as the worker takes it, or None."""
    return service.login_for(request.app.state.settings, conn, node_id, url)


@router.get("/logins", summary="Git hosts this node has a login for")
def list_logins(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return each host and user name. The token is never returned."""
    authorise(conn, registry, who, node_id, "git.credentials")
    return {"logins": repository.list_logins(conn, node_id)}


@router.put("/logins", status_code=204, summary="Save the login for a Git host")
def save_login(
    node_id: str,
    body: LoginIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Keep (encrypted) the access token used to reach private repositories of one host.

    It is sent only to repositories of exactly that host. Replaces an earlier login. The audit
    entry names the host and the user, never the token.
    """
    authorise(conn, registry, who, node_id, "git.credentials")
    host = body.host.lower()
    sealed = crypto.encrypt(request.app.state.settings, body.secret)
    if not repository.save_login(conn, node_id, host, body.username, sealed, who.actor):
        raise ApiError("git.too_many_logins", 409)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "git.login_save",
        object_type="node",
        object_id=node_id,
        details={"host": host, "username": body.username[:40]},
    )


@router.delete("/logins/{host}", status_code=204, summary="Forget the login for a Git host")
def delete_login(
    node_id: str,
    host: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove the stored login of one host."""
    authorise(conn, registry, who, node_id, "git.credentials")
    name = host.lower()
    if not repository.delete_login(conn, node_id, name):
        raise ApiError("git.no_such_login", 404)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "git.login_delete",
        object_type="node",
        object_id=node_id,
        details={"host": name},
    )


@router.post("/clone", status_code=201, summary="Make a project from a Git repository")
async def clone(
    node_id: str,
    body: CloneIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Copy the branch into a new project folder and remember where it came from.

    Needs `projects.create`. Nothing is started. The audit entry names the project, the address
    and the branch, never the login.
    """
    authorise(conn, registry, who, node_id, "projects.create")
    details = {"project": body.name, "url": body.url, "branch": body.branch}
    claimed = states.claim(
        conn, node_id, body.name
    )  # managed from the start, like one created here
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "git.clone",
            {
                "project": body.name,
                "url": body.url,
                "branch": body.branch,
                "login": _login_for(request, conn, node_id, body.url),
            },
            schemas.Commit,
        )
    except ApiError as error:
        if claimed:
            states.forget(conn, node_id, body.name)
        audit_failure(conn, registry, who, "git.clone", node_id, details, error)
        raise
    repository.set_source(conn, node_id, body.name, body.url, body.branch, who.actor)
    conn.commit()
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "git.clone",
        object_type="node",
        object_id=node_id,
        details=details,
    )
    return {"project": body.name, **answer.model_dump()}


@router.get("/projects/{project}", summary="A project's Git source and how it compares")
async def source(
    node_id: str,
    project: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Return where the project came from and whether the branch has a newer commit.

    `source` is null for a project that did not come from Git. If the node cannot be asked,
    `error` holds the code and the saved address is still returned.
    """
    _project(project)
    authorise(conn, registry, who, node_id, "projects.read", ("project", project))
    states.require_managed(conn, node_id, project)
    saved = repository.get_source(conn, node_id, project)
    if saved is None:
        return {"source": None, "status": None, "error": None}
    try:
        status = await ask(
            request,
            registry,
            node_id,
            "git.status",
            {
                "project": project,
                "url": saved["url"],
                "branch": saved["branch"],
                "login": _login_for(request, conn, node_id, saved["url"]),
            },
            schemas.Status,
        )
    except ApiError as error:
        return {"source": saved, "status": None, "error": error.code}
    return {"source": saved, "status": status.model_dump(), "error": None}


@router.post("/projects/{project}/update", summary="Bring a project to the branch's newest commit")
async def update(
    node_id: str,
    project: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Fetch the branch and replace the project's tracked files with it.

    Refused if files were edited by hand. Needs `projects.files.write`. It does not deploy.
    """
    _project(project)
    authorise(conn, registry, who, node_id, "projects.files.write", ("project", project))
    states.require_managed(conn, node_id, project)
    saved = repository.get_source(conn, node_id, project)
    if saved is None:
        raise ApiError("git.no_source", 404)
    details = {"project": project, "url": saved["url"], "branch": saved["branch"]}
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "git.pull",
            {
                "project": project,
                "url": saved["url"],
                "branch": saved["branch"],
                "login": _login_for(request, conn, node_id, saved["url"]),
            },
            schemas.Commit,
        )
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
    return answer.model_dump()


@router.delete("/projects/{project}", status_code=204, summary="Forget a project's Git source")
def forget(
    node_id: str,
    project: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Stop following the repository. The files stay where they are."""
    _project(project)
    authorise(conn, registry, who, node_id, "projects.files.write", ("project", project))
    states.require_managed(conn, node_id, project)
    if not repository.delete_source(conn, node_id, project):
        raise ApiError("git.no_source", 404)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "git.forget",
        object_type="node",
        object_id=node_id,
        details={"project": project},
    )
