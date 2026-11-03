"""Project endpoints of a node: list, read a Compose file, save a Compose file."""

from __future__ import annotations

import hashlib
import re
import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.projects import schemas
from armadok.modules.workers.routes import call_operation

router = APIRouter(prefix="/nodes/{node_id}/projects", tags=["projects"])

_PROJECT = re.compile(schemas.PROJECT_PATTERN)
_FILE = re.compile(schemas.FILE_PATTERN)


def _authorise(
    conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str, right: str
) -> None:
    """Allow only callers who can see the node and hold `right` on it."""
    scope = f"node:{node_id}"
    catalogue = registry.rights()
    if nodes_repo.get_node(conn, node_id) is None or not who.has("nodes.read", catalogue, scope):
        raise NotFound()
    who.require(right, catalogue, scope)


def _names(project: str, file: str) -> None:
    """Refuse a malformed name before it goes anywhere near a worker."""
    if not _PROJECT.fullmatch(project) or not _FILE.fullmatch(file):
        raise ApiError("projects.invalid_name", 422)


async def _ask(request: Request, registry: Registry, node_id: str, op: str, args: dict, model):
    result = await call_operation(request.app, registry, node_id, op, args)
    try:
        return model.model_validate(result)
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None


def _audit_failure(conn, registry, who, event, node_id, project, file, error: ApiError) -> None:
    """Record a refused or failed attempt: a symlink refusal or a timeout is worth a trace.

    A write that times out may still have happened on the machine, so it must be recorded
    even though the caller got an error.
    """
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        outcome="failure",
        details={"project": project, "file": file, "code": error.code},
    )


@router.get("", summary="Compose projects of the machine")
async def list_projects(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the project folders found in the machine's allowed folder, with their files."""
    _authorise(conn, registry, who, node_id, "projects.read")
    answer = await _ask(request, registry, node_id, "projects.list", {}, schemas.ProjectList)
    return answer.model_dump()


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
    _authorise(conn, registry, who, node_id, "projects.files.read")
    _names(project, file)
    try:
        answer = await _ask(
            request,
            registry,
            node_id,
            "projects.file.read",
            {"project": project, "file": file},
            schemas.FileContent,
        )
    except ApiError as error:
        _audit_failure(conn, registry, who, "project.file_read", node_id, project, file, error)
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
    _authorise(conn, registry, who, node_id, "projects.files.write")
    _names(project, file)
    sent = body.content.encode()
    try:
        answer = await _ask(
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
        _audit_failure(conn, registry, who, "project.file_write", node_id, project, file, error)
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
