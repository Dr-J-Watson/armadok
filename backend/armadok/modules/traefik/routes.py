"""Traefik endpoints of a node: files (list, read, save) and the sites they define."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3

from fastapi import APIRouter, Depends, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traefik import coherence, parse, schemas
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/traefik", tags=["traefik"])

_PATH = re.compile(schemas.PATH_PATTERN)


def _checked_path(path: str) -> str:
    """Refuse a malformed path before it goes anywhere near a worker."""
    if not _PATH.fullmatch(path):
        raise ApiError("traefik.invalid_path", 422)
    return path


def require_for_sites_folder(who: Principal, registry: Registry, node_id: str, path: str) -> None:
    """Files of `sites/` route real traffic: writing them takes the sites right as well.

    Otherwise a holder of the plain file-editing right (which API keys may carry) could do
    through the generic editor what the sensitive sites right forbids.
    """
    # `middlewares/` holds what decides who reaches a site (see `middleware_routes`).
    if path.split("/", 1)[0] in ("sites", "middlewares"):
        who.require("traefik.sites.write", registry.rights(), f"node:{node_id}")


@router.get("/files", summary="Traefik configuration files")
async def list_files(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the dynamic configuration files of the node's Traefik folder."""
    authorise(conn, registry, who, node_id, "traefik.read")
    answer = await ask(request, registry, node_id, "traefik.files.list", {}, schemas.FileList)
    return answer.model_dump()


@router.get("/sites", summary="Routers, services and middlewares defined in the files")
async def sites(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return what the configuration files define, read from all files at once."""
    authorise(conn, registry, who, node_id, "traefik.read")
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    # Parsing is slow Python: run it off the event loop so one request cannot stall the hub.
    summaries = await asyncio.to_thread(
        lambda: [parse.summarize(f.path, f.content, f.sha256) for f in snapshot.files]
    )
    return {
        "routers": [r for s in summaries for r in s["routers"]],
        "services": [x for s in summaries for x in s["services"]],
        "middlewares": [m for s in summaries for m in s["middlewares"]],
        "errors": [{"path": s["path"], "error": s["error"]} for s in summaries if s["error"]],
        "truncated": snapshot.truncated,
    }


@router.get("/coherence", summary="Check that the configuration files agree with each other")
async def coherence_check(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return broken references, loops and leftovers found across all the files.

    Read-only: nothing is changed. Needs the same right as the sites view.
    """
    authorise(conn, registry, who, node_id, "traefik.read")
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    summaries = await asyncio.to_thread(
        lambda: [parse.summarize(f.path, f.content, f.sha256) for f in snapshot.files]
    )
    issues = await asyncio.to_thread(coherence.analyse, summaries)
    return {
        "issues": issues,
        "errors": [{"path": s["path"], "error": s["error"]} for s in summaries if s["error"]],
        "truncated": snapshot.truncated,
    }


@router.get("/files/{path:path}", summary="Read a configuration file")
async def read_file(
    node_id: str,
    path: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the file's text and its version hash. Reads are audited (files can hold secrets)."""
    authorise(conn, registry, who, node_id, "traefik.files.read")
    _checked_path(path)
    try:
        answer = await ask(
            request, registry, node_id, "traefik.file.read", {"path": path}, schemas.FileContent
        )
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.file_read", node_id, {"path": path}, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "traefik.file_read",
        object_type="node",
        object_id=node_id,
        details={"path": path},
    )
    return answer.model_dump()


@router.put("/files/{path:path}", summary="Save a configuration file")
async def write_file(
    node_id: str,
    path: str,
    body: schemas.WriteIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Save a file after checking it is valid YAML. 409 if it changed since `expected_sha256`.

    Refusing broken YAML here protects the running Traefik: it would drop the whole file.
    """
    authorise(conn, registry, who, node_id, "traefik.files.write")
    _checked_path(path)
    require_for_sites_folder(who, registry, node_id, path)
    try:
        await asyncio.to_thread(parse.load, body.content)
    except parse.YamlError:
        raise ApiError("traefik.invalid_yaml", 422) from None
    sent = body.content.encode()
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "traefik.file.write",
            {"path": path, "content": body.content, "expected_sha256": body.expected_sha256},
            schemas.Saved,
        )
        if answer.size != len(sent) or answer.sha256 != hashlib.sha256(sent).hexdigest():
            raise ApiError("worker.invalid_answer", 502)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.file_write", node_id, {"path": path}, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "traefik.file_write",
        object_type="node",
        object_id=node_id,
        details={"path": path, "bytes": len(sent)},
    )
    return answer.model_dump()
