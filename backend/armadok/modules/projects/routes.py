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
from armadok.modules.projects import schemas
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/projects", tags=["projects"])

_PROJECT = re.compile(schemas.PROJECT_PATTERN)
_FILE = re.compile(schemas.FILE_PATTERN)


def _names(project: str, file: str) -> None:
    """Refuse a malformed name before it goes anywhere near a worker."""
    if not _PROJECT.fullmatch(project) or not _FILE.fullmatch(file):
        raise ApiError("projects.invalid_name", 422)


@router.get("", summary="Compose projects of the machine")
async def list_projects(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the project folders found in the machine's allowed folder, with their files."""
    authorise(conn, registry, who, node_id, "projects.read")
    answer = await ask(request, registry, node_id, "projects.list", {}, schemas.ProjectList)
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
    _names(project, file)
    authorise(conn, registry, who, node_id, "projects.files.read", ("project", project))
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
