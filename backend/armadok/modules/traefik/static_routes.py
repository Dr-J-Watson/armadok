"""Traefik's static configuration (`traefik.yml`): read it, change it, go back to an older copy.

The file holds what Traefik reads only at start: entry points, certificate resolvers, the API,
the access log and the metrics. A mistake here stops the proxy, and it can expose the API, so
changing it takes its own sensitive right (never for API keys). Reading it takes the same right
as reading any configuration file (it can hold tokens). Every version replaced is kept on the
node. Armadok writes the file; Traefik is restarted afterwards, which the person confirms.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traefik import parse, schemas
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/traefik/static", tags=["traefik"])
Version = Path(pattern=r"^[0-9]{13}\.[0-9a-f]{8}$")


class StaticFile(BaseModel):
    """Answer of `traefik.static.read` (untrusted: the hash is recomputed here)."""

    model_config = ConfigDict(extra="ignore", strict=True)
    configured: bool
    name: str | None = Field(default=None, pattern=r"^traefik\.ya?ml$")
    content: str | None = Field(default=None, max_length=schemas.MAX_FILE_BYTES)
    sha256: str | None = Field(default=None, pattern=schemas.SHA_PATTERN)
    size: int | None = Field(default=None, ge=0, le=schemas.MAX_FILE_BYTES)

    @model_validator(mode="after")
    def complete_and_consistent(self) -> StaticFile:
        """When configured, all fields are there and the hash and size match the text."""
        if not self.configured:
            return self
        if self.name is None or self.content is None or self.sha256 is None or self.size is None:
            raise ValueError("incomplete answer")
        data = self.content.encode()
        if self.size != len(data) or self.sha256 != hashlib.sha256(data).hexdigest():
            raise ValueError("hash or size does not match the content")
        return self


class StaticWrite(BaseModel):
    """Body of a save: the new text and the version being replaced."""

    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=schemas.MAX_FILE_BYTES)
    expected_sha256: str = Field(pattern=schemas.SHA_PATTERN)

    @model_validator(mode="after")
    def fits_in_bytes(self) -> StaticWrite:
        """Enforce the limit in bytes, not characters, before anything is parsed."""
        if len(self.content.encode()) > schemas.MAX_FILE_BYTES:
            raise ValueError("file too large")
        return self


def _record(conn, registry, who, event, node_id, **info) -> None:
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        details=info,
    )


@router.get("", summary="Traefik's static configuration")
async def read_static(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the file, or `configured: false` when the node has no static folder set up.

    Needs the right to read configuration files. Reads are audited (the file can hold tokens).
    """
    authorise(conn, registry, who, node_id, "traefik.files.read")
    answer = await ask(request, registry, node_id, "traefik.static.read", {}, StaticFile)
    if answer.configured:
        _record(conn, registry, who, "traefik.static_read", node_id)
    return answer.model_dump()


@router.put("", summary="Save Traefik's static configuration")
async def write_static(
    node_id: str,
    body: StaticWrite,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Save the file after checking it is a YAML mapping; 409 if it changed since it was read.

    Traefik keeps running with the old text until it is restarted, so the change is not live yet.
    """
    authorise(conn, registry, who, node_id, "traefik.static.write")
    try:
        document: Any = await asyncio.to_thread(parse.load, body.content)
    except parse.YamlError:
        raise ApiError("traefik.invalid_yaml", 422) from None
    if not isinstance(document, dict) or not document:
        # An empty or non-mapping file would start Traefik with nothing: never write that.
        raise ApiError("traefik.invalid_yaml", 422)
    sent = body.content.encode()
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "traefik.static.write",
            {"content": body.content, "expected_sha256": body.expected_sha256},
            schemas.Saved,
        )
        if answer.size != len(sent) or answer.sha256 != hashlib.sha256(sent).hexdigest():
            raise ApiError("worker.invalid_answer", 502)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.static_write", node_id, {}, error)
        raise
    _record(conn, registry, who, "traefik.static_write", node_id, bytes=len(sent))
    return answer.model_dump()


@router.get("/history", summary="Kept versions of the static configuration")
async def static_history(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the versions kept on the node, newest first."""
    authorise(conn, registry, who, node_id, "traefik.files.read")
    answer = await ask(
        request, registry, node_id, "traefik.static.history.list", {}, schemas.Versions
    )
    return answer.model_dump()


@router.get("/history/{version}", summary="One kept version of the static configuration")
async def static_version(
    node_id: str,
    request: Request,
    version: str = Version,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the text of an older version, to compare with the current one or put back."""
    authorise(conn, registry, who, node_id, "traefik.files.read")
    answer = await ask(
        request,
        registry,
        node_id,
        "traefik.static.history.read",
        {"version": version},
        schemas.FileContent,
    )
    _record(conn, registry, who, "traefik.static_history_read", node_id, version=version)
    return answer.model_dump()
