"""Item-level endpoints of a node's Traefik files: switch an item on or off, read or change one.

Everything works on the text of one item, never on a whole re-dumped file (see `itemfile`), so
comments and ordering survive. Changing what routes traffic needs the sensitive sites right as
well as the file-editing one. Content never reaches the audit log: names and paths only.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traefik import itemfile, parse, schemas
from armadok.modules.traefik.routes import _checked_path, require_for_sites_folder
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/traefik/items", tags=["traefik"])

ITEM_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
Kind = Literal["routers", "middlewares"]


class ToggleIn(BaseModel):
    """Which item to switch on or off."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(pattern=schemas.PATH_PATTERN)
    kind: Kind
    name: str = Field(pattern=ITEM_NAME)
    enabled: bool


class ItemIn(BaseModel):
    """The new value (YAML text) of one item, and the version of the file it was read from."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(pattern=schemas.PATH_PATTERN)
    kind: Kind
    name: str = Field(pattern=ITEM_NAME)
    yaml: str = Field(min_length=1, max_length=64 * 1024)
    expected_sha256: str | None = Field(default=None, pattern=schemas.SHA_PATTERN)


class NewItemIn(BaseModel):
    """A new item: in an existing file (with its version), or in a file of its own."""

    model_config = ConfigDict(extra="forbid")
    kind: Kind
    name: str = Field(pattern=ITEM_NAME)
    yaml: str = Field(min_length=1, max_length=64 * 1024)
    path: str | None = Field(default=None, pattern=schemas.PATH_PATTERN)
    expected_sha256: str | None = Field(default=None, pattern=schemas.SHA_PATTERN)


async def _snapshot(
    request: Request, registry: Registry, node_id: str
) -> dict[str, tuple[str, str]]:
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    return {f.path: (f.content, f.sha256) for f in snapshot.files}


async def _save(
    request: Request, registry: Registry, node_id: str, path: str, text: str, expected: str | None
) -> None:
    sent = text.encode()
    answer = await ask(
        request,
        registry,
        node_id,
        "traefik.file.write",
        {"path": path, "content": text, "expected_sha256": expected},
        schemas.Saved,
    )
    if answer.size != len(sent) or answer.sha256 != hashlib.sha256(sent).hexdigest():
        raise ApiError("worker.invalid_answer", 502)


def _error(error: itemfile.ItemError) -> ApiError:
    code = str(error)
    if code == "not_found":
        return NotFound()
    return ApiError(
        {
            "invalid_yaml": "traefik.invalid_yaml",
            "invalid_item": "traefik.invalid_item",
            "exists": "traefik.item_exists",
            "unsupported_layout": "traefik.unsupported_layout",
        }.get(code, "traefik.invalid_item"),
        409 if code in ("exists", "unsupported_layout") else 422,
    )


def _record(conn, registry, who, event: str, node_id: str, **details) -> None:
    audit.record(
        conn, registry.audit_events(), who.actor, event,
        object_type="node", object_id=node_id, details=details,
    )  # fmt: skip


@router.get("/overview", summary="Routers, services and middlewares, switched-off ones included")
async def items_overview(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return what the files define, each item flagged `disabled` when switched off here."""
    authorise(conn, registry, who, node_id, "traefik.read")
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    summaries = await asyncio.to_thread(
        lambda: [itemfile.overview(f.path, f.content, f.sha256) for f in snapshot.files]
    )
    return {
        "routers": [r for s in summaries for r in s["routers"]],
        "services": [x for s in summaries for x in s["services"]],
        "middlewares": [m for s in summaries for m in s["middlewares"]],
        "errors": [{"path": s["path"], "error": s["error"]} for s in summaries if s["error"]],
        "truncated": snapshot.truncated,
    }


@router.post("/toggle", summary="Switch a router or a middleware on or off")
async def toggle_item(
    node_id: str,
    body: ToggleIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Comment an item out (or put it back).

    A switched-off middleware is also taken out of the routers and chains that list it, and put
    back with it. The item's file is written first, so a failure halfway leaves routers without
    the middleware (they stop) rather than unprotected.
    """
    authorise(conn, registry, who, node_id, "traefik.files.write")
    who.require("traefik.sites.write", registry.rights(), f"node:{node_id}")
    _checked_path(body.path)
    details = {"path": body.path, "kind": body.kind, "name": body.name, "enabled": body.enabled}
    try:
        files = await _snapshot(request, registry, node_id)
        if body.path not in files:
            raise NotFound()
        new = {path: text for path, (text, _) in files.items()}
        try:
            span = itemfile.find(new[body.path], "http", body.kind, body.name)
        except itemfile.ItemError as error:
            raise _error(error) from error
        if span.disabled != body.enabled:
            return {"changed": []}  # already as asked
        new[body.path] = itemfile.set_enabled(new[body.path], span, body.enabled)
        if body.kind == "middlewares":
            for path, text in list(new.items()):
                lines, flow = itemfile.references(text, body.name)
                if flow and not body.enabled:
                    raise ApiError("traefik.flow_reference", 409)
                if lines:
                    new[path] = itemfile.set_references(text, lines, body.enabled)
        changed = [p for p in new if new[p] != files[p][0]]
        # The file holding the item first, then the files that list it.
        changed.sort(key=lambda p: p != body.path)
        for path in changed:
            await asyncio.to_thread(parse.load, new[path])
            await _save(request, registry, node_id, path, new[path], files[path][1])
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.item_toggle", node_id, details, error)
        raise
    _record(conn, registry, who, "traefik.item_toggle", node_id, **details, files=len(changed))
    return {"changed": changed}


@router.get("", summary="One item as YAML text")
async def read_item(
    node_id: str,
    path: str,
    kind: Kind,
    name: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the item's value as YAML and the version of its file. Audited like a file read."""
    authorise(conn, registry, who, node_id, "traefik.files.read")
    _checked_path(path)
    files = await _snapshot(request, registry, node_id)
    if path not in files:
        raise NotFound()
    text, sha = files[path]
    try:
        span = itemfile.find(text, "http", kind, name)
        value = itemfile.item_yaml(text, span)
    except itemfile.ItemError as error:
        raise _error(error) from error
    _record(conn, registry, who, "traefik.file_read", node_id, path=path)
    return {"yaml": value, "sha256": sha, "disabled": span.disabled}


@router.put("", summary="Change one item")
async def write_item(
    node_id: str,
    body: ItemIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Replace the item's lines with the given YAML; the rest of the file is left as it was."""
    authorise(conn, registry, who, node_id, "traefik.files.write")
    _checked_path(body.path)
    require_for_sites_folder(who, registry, node_id, body.path)
    details = {"path": body.path, "kind": body.kind, "name": body.name}
    try:
        files = await _snapshot(request, registry, node_id)
        if body.path not in files:
            raise NotFound()
        text, sha = files[body.path]
        try:
            span = itemfile.find(text, "http", body.kind, body.name)
            new = itemfile.replace(text, span, body.yaml)
            await asyncio.to_thread(parse.load, new)
        except itemfile.ItemError as error:
            raise _error(error) from error
        except parse.YamlError:
            raise ApiError("traefik.invalid_yaml", 422) from None
        await _save(request, registry, node_id, body.path, new, body.expected_sha256 or sha)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.item_write", node_id, details, error)
        raise
    _record(conn, registry, who, "traefik.item_write", node_id, **details)
    return {"sha256": hashlib.sha256(new.encode()).hexdigest()}


@router.post("", status_code=201, summary="Add an item")
async def add_item(
    node_id: str,
    body: NewItemIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Add an item to an existing file, or make `middlewares/<name>.yml` for a new middleware."""
    authorise(conn, registry, who, node_id, "traefik.files.write")
    who.require("traefik.sites.write", registry.rights(), f"node:{node_id}")
    details = {
        "path": body.path or f"middlewares/{body.name}.yml",
        "kind": body.kind,
        "name": body.name,
    }
    try:
        if body.path is None:
            if (
                body.kind != "middlewares"
                or not body.name.islower()
                or "." in body.name
                or "_" in body.name
            ):
                raise ApiError("traefik.invalid_name", 422)
            try:
                new = itemfile.add("", "http", body.kind, body.name, body.yaml)
                await asyncio.to_thread(parse.load, new)
            except itemfile.ItemError as error:
                raise _error(error) from error
            except parse.YamlError:
                raise ApiError("traefik.invalid_yaml", 422) from None
            answer = await ask(
                request, registry, node_id, "traefik.middleware.write",
                {"name": body.name, "content": new, "expected_sha256": None},
                schemas.Saved,
            )  # fmt: skip
            if answer.size != len(new.encode()):
                raise ApiError("worker.invalid_answer", 502)
        else:
            _checked_path(body.path)
            files = await _snapshot(request, registry, node_id)
            if body.path not in files:
                raise NotFound()
            text, sha = files[body.path]
            try:
                new = itemfile.add(text, "http", body.kind, body.name, body.yaml)
                await asyncio.to_thread(parse.load, new)
            except itemfile.ItemError as error:
                raise _error(error) from error
            except parse.YamlError:
                raise ApiError("traefik.invalid_yaml", 422) from None
            await _save(request, registry, node_id, body.path, new, body.expected_sha256 or sha)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.item_create", node_id, details, error)
        raise
    _record(conn, registry, who, "traefik.item_create", node_id, **details)
    return {"path": details["path"], "name": body.name}


@router.delete("", status_code=204, summary="Remove an item")
async def delete_item(
    node_id: str,
    path: str,
    kind: Kind,
    name: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove the item from its file. Refused while a router or chain still lists a middleware."""
    authorise(conn, registry, who, node_id, "traefik.files.write")
    who.require("traefik.sites.write", registry.rights(), f"node:{node_id}")
    _checked_path(path)
    details = {"path": path, "kind": kind, "name": name}
    try:
        files = await _snapshot(request, registry, node_id)
        if path not in files:
            raise NotFound()
        text, sha = files[path]
        try:
            span = itemfile.find(text, "http", kind, name)
        except itemfile.ItemError as error:
            raise _error(error) from error
        if kind == "middlewares" and not span.disabled:
            for content, _ in files.values():
                if itemfile.references(content, name)[0] or itemfile.references(content, name)[1]:
                    raise ApiError("traefik.in_use", 409)
        new = itemfile.remove(text, span)
        await asyncio.to_thread(parse.load, new or "{}")
        await _save(request, registry, node_id, path, new, sha)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.item_delete", node_id, details, error)
        raise
    _record(conn, registry, who, "traefik.item_delete", node_id, **details)
