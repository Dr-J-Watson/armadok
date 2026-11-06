"""Project endpoints of a node: list, read a Compose file, save a Compose file."""

from __future__ import annotations

import hashlib
import re
import sqlite3

from fastapi import APIRouter, Depends, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.projects import schemas, states
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/projects", tags=["projects"])

_PROJECT = re.compile(schemas.PROJECT_PATTERN)
_FILE = re.compile(schemas.FILE_PATTERN)


def _names(project: str, file: str) -> None:
    """Refuse a malformed name before it goes anywhere near a worker."""
    if not _PROJECT.fullmatch(project) or not _FILE.fullmatch(file):
        raise ApiError("projects.invalid_name", 422)


@router.get("", summary="Compose projects of the node")
async def list_projects(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the project folders found in the node's allowed folder, with their files."""
    authorise(conn, registry, who, node_id, "projects.read")
    answer = await ask(request, registry, node_id, "projects.list", {}, schemas.ProjectList)
    if not answer.truncated:
        states.prune(conn, node_id, {p.name for p in answer.projects})
    known = states.all_states(conn, node_id)
    managed = [
        {**p.model_dump(), "state": "managed", "labels": known[p.name][1]}
        for p in answer.projects
        if p.name in known and known[p.name][0] == "managed"
    ]
    return {"projects": managed, "truncated": answer.truncated}


@router.get("/discovered", summary="Folders with a Compose file that are not imported")
async def discovered_projects(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """List what could be imported (discovered) and what was set aside (ignored).

    Needs `projects.import`. Read only: the folders are not opened beyond the first Compose file
    needed to name their services.
    """
    authorise(conn, registry, who, node_id, "projects.import")
    answer = await ask(request, registry, node_id, "projects.list", {}, schemas.ProjectList)
    if not answer.truncated:
        states.prune(conn, node_id, {p.name for p in answer.projects})
    known = states.all_states(conn, node_id)
    found = [
        {**p.model_dump(), "state": known[p.name][0] if p.name in known else "discovered"}
        for p in answer.projects
        if p.name not in known or known[p.name][0] != "managed"
    ]
    return {"projects": found, "truncated": answer.truncated}


async def _existing(request: Request, registry: Registry, node_id: str, names: list[str]) -> None:
    """Refuse names that are not folders with a Compose file on the node right now."""
    answer = await ask(request, registry, node_id, "projects.list", {}, schemas.ProjectList)
    present = {p.name for p in answer.projects}
    if any(name not in present for name in names):
        raise ApiError("projects.not_found", 404)


async def _existing_audited(
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    event: str,
    request: Request,
    node_id: str,
    names: list[str],
) -> None:
    """`_existing`, recording a refusal in the audit log (with how many, never a guess at names)."""
    try:
        await _existing(request, registry, node_id, names)
    except ApiError as error:
        audit_failure(conn, registry, who, event, node_id, {"projects": len(names)}, error)
        raise


def _record(conn, registry, who, event: str, node_id: str, names: list[str], **extra) -> None:
    """Write one audit entry per project (names and labels, never content)."""
    for name in names:
        audit.record(
            conn,
            registry.audit_events(),
            who.actor,
            event,
            object_type="node",
            object_id=node_id,
            details={"project": name, **extra},
        )


@router.post("/import", summary="Manage discovered projects")
async def import_projects(
    node_id: str,
    body: schemas.ImportIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Make the folders managed projects (with labels). Disk and containers are not touched."""
    authorise(conn, registry, who, node_id, "projects.import")
    await _existing_audited(conn, registry, who, "project.import", request, node_id, body.projects)
    labels = states.clean_labels(body.labels)
    states.set_state(conn, node_id, body.projects, "managed", labels)
    _record(conn, registry, who, "project.import", node_id, body.projects, labels=labels)
    return {"imported": body.projects}


@router.post("/ignore", summary="Set discovered projects aside")
async def ignore_projects(
    node_id: str,
    body: schemas.ImportIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Hide the folders from the import list. A managed project is never ignored this way."""
    authorise(conn, registry, who, node_id, "projects.import")
    await _existing_audited(conn, registry, who, "project.ignore", request, node_id, body.projects)
    known = states.all_states(conn, node_id)
    if any(known.get(name, ("", []))[0] == "managed" for name in body.projects):
        error = ApiError("projects.already_managed", 409)
        audit_failure(
            conn, registry, who, "project.ignore", node_id, {"projects": len(body.projects)}, error
        )
        raise error
    states.set_state(conn, node_id, body.projects, "ignored")
    _record(conn, registry, who, "project.ignore", node_id, body.projects)
    return {"ignored": body.projects}


@router.post("/{project}/unmanage", status_code=204, summary="Stop managing a project")
async def unmanage_project(
    node_id: str,
    project: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Take a project out of Armadok, or bring an ignored one back to the discovered list.

    Neither the folder nor its containers are touched: it only stops being shown and usable here.
    """
    authorise(conn, registry, who, node_id, "projects.import")
    _names(project, "compose.yaml")
    previous = states.forget(conn, node_id, project)
    if previous is None:
        raise ApiError("projects.not_found", 404)
    _record(
        conn,
        registry,
        who,
        "project.unmanage" if previous == "managed" else "project.restore",
        node_id,
        [project],
    )


@router.get("/{project}/files/{file}", summary="Read a Compose file")
async def read_file(
    node_id: str,
    project: str,
    file: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the file's text and its version hash. Reads are audited (files can hold secrets)."""
    _names(project, file)
    authorise(conn, registry, who, node_id, "projects.files.read", ("project", project))
    states.require_managed(conn, node_id, project)
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "projects.file.read",
            {"project": project, "file": file},
            schemas.FileContent,
        )
    except ApiError as error:
        audit_failure(
            conn,
            registry,
            who,
            "project.file_read",
            node_id,
            {"project": project, "file": file},
            error,
        )
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "project.file_read",
        object_type="node",
        object_id=node_id,
        details={"project": project, "file": file},
    )
    return answer.model_dump()


@router.put("/{project}/files/{file}", summary="Save a Compose file")
async def write_file(
    node_id: str,
    project: str,
    file: str,
    body: schemas.WriteIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Replace a Compose file. Fails with 409 if it changed since `expected_sha256`.

    The audit entry records which file and how many bytes, never the content.
    """
    _names(project, file)
    authorise(conn, registry, who, node_id, "projects.files.write", ("project", project))
    states.require_managed(conn, node_id, project)
    sent = body.content.encode()
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "projects.file.write",
            {
                "project": project,
                "file": file,
                "content": body.content,
                "expected_sha256": body.expected_sha256,
            },
            schemas.Saved,
        )
        # What the worker says it wrote must be what we sent.
        if answer.size != len(sent) or answer.sha256 != hashlib.sha256(sent).hexdigest():
            raise ApiError("worker.invalid_answer", 502)
    except ApiError as error:
        audit_failure(
            conn,
            registry,
            who,
            "project.file_write",
            node_id,
            {"project": project, "file": file},
            error,
        )
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "project.file_write",
        object_type="node",
        object_id=node_id,
        details={"project": project, "file": file, "bytes": len(sent)},
    )
    return answer.model_dump()


@router.post("", status_code=201, summary="Create a project")
async def create_project(
    node_id: str,
    body: schemas.CreateIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Make a new project folder with a Compose file and, if given, a private `.env`.

    Needs `projects.create`. The folder must not exist. Nothing is run: deploying is a separate
    action with its own right. The `.env` text goes to the node and nowhere else (it is
    never stored here, returned or audited); the audit entry only says whether one was given.
    """
    authorise(conn, registry, who, node_id, "projects.create")
    details = {"project": body.name, "env": body.env is not None}
    claimed = states.claim(
        conn, node_id, body.name
    )  # a project made here is managed from the start
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "projects.create",
            {"project": body.name, "compose": body.compose, "env": body.env},
            schemas.Created,
        )
        if (answer.project, answer.file, answer.env) != (
            body.name,
            "compose.yaml",
            body.env is not None,
        ):
            raise ApiError("worker.invalid_answer", 502)
    except ApiError as error:
        if claimed:
            states.forget(conn, node_id, body.name)
        audit_failure(conn, registry, who, "project.create", node_id, details, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "project.create",
        object_type="node",
        object_id=node_id,
        details=details,
    )
    return answer.model_dump()
