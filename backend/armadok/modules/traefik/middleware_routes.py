"""Middleware endpoints of a node: create, change, list and delete middlewares by intention.

A middleware is one generated file in `middlewares/` (see `middlewares`). Needs
`traefik.sites.write` (sensitive) to change anything. Passwords are hashed here and never
stored, logged or returned. Every change is audited by name and intention, never by content.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traefik import middlewares as builder
from armadok.modules.traefik import parse, schemas, sitebuilder
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/traefik/middlewares", tags=["traefik"])
MiddlewareName = Path(pattern=builder.NAME_PATTERN, description="Middleware name")


def _path(name: str) -> str:
    return f"{builder.FOLDER}/{name}.yml"


async def _snapshot(request: Request, registry: Registry, node_id: str) -> schemas.Snapshot:
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    if snapshot.truncated:
        # Names or users of files we could not see might collide: better to refuse than to guess.
        raise ApiError("traefik.snapshot_incomplete", 409)
    return snapshot


async def _summaries(snapshot: schemas.Snapshot) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        lambda: [parse.summarize(f.path, f.content, f.sha256) for f in snapshot.files]
    )


def _public(spec: builder.Spec) -> dict:
    """Return the description as the form shows it: no hash, only who has a password."""
    data = spec.model_dump()
    for user in data.get("users", []):
        user["has_password"] = bool(user.pop("hash", None))
    return data


def _size_of(spec: builder.Spec) -> dict:
    """Return counts worth auditing (how many users or networks), never names or values."""
    if isinstance(spec, builder.BasicAuth):
        return {"users": len(spec.users), "new_passwords": sum(1 for u in spec.users if u.password)}
    if isinstance(spec, builder.LocalOnly):
        return {"networks": len(spec.ranges)}
    return {}


def _writer(conn, registry, who, node_id) -> None:
    """Allow changes only to those who may change sites.

    A middleware decides who may reach a site (a network, a password, a login service): whoever
    can rewrite it can open any site that uses it. So it takes the sensitive sites right, which
    API keys cannot hold, not the plain file-editing right.
    """
    authorise(conn, registry, who, node_id, "traefik.sites.write")


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


@router.get("", summary="Middlewares built with the form")
async def list_middlewares(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the generated middlewares, and which routers use each."""
    authorise(conn, registry, who, node_id, "traefik.read")
    detailed = who.has("traefik.files.read", registry.rights(), f"node:{node_id}")
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    summaries = await _summaries(snapshot)
    routers = [r for s in summaries for r in s["routers"]]
    found = []
    for item in snapshot.files:
        if not item.path.startswith(f"{builder.FOLDER}/"):
            continue
        spec = builder.spec_of(item.content)
        if spec is None:
            continue
        ref = builder.reference(spec.name)
        found.append(
            {
                "name": spec.name,
                "path": item.path,
                "sha256": item.sha256,
                # Users, networks and addresses are configuration: for those who may read files.
                "spec": _public(spec) if detailed else {"intent": spec.intent, "name": spec.name},
                "editable": builder.untouched(item.content) is not None,
                "used_by": sorted(r["name"] for r in routers if ref in r["middlewares"]),
            }
        )
    return {"middlewares": sorted(found, key=lambda m: m["name"]), "truncated": snapshot.truncated}


async def _check_free(summaries: list[dict], name: str, own_path: str | None) -> None:
    """Refuse a name already used by a middleware of another file."""
    for summary in summaries:
        if summary["path"] == own_path:
            continue
        if name in {m["name"] for m in summary["middlewares"]}:
            raise ApiError("traefik.name_taken", 409)


# Hashing is slow on purpose: a few at a time, off the event loop, so it can neither stall the
# hub nor be used to make it busy.
_hashing = asyncio.Semaphore(2)


async def _resolved(spec: builder.Spec, previous: builder.Spec | None) -> builder.Spec:
    """Hash the passwords of a description (everything is checked before any hashing)."""
    try:
        builder.check_passwords(spec, previous)
        async with _hashing:
            return await asyncio.to_thread(builder.resolve, spec, previous)
    except ValueError as error:
        message = str(error)
        code = (
            "traefik.password_needed"
            if "needed" in message
            else "traefik.too_many_passwords"
            if "too many" in message
            else "traefik.invalid_password"
        )
        raise ApiError(code, 422) from None


@router.post("/preview", summary="Show the file a middleware description would produce")
async def preview(
    node_id: str,
    body: builder.SpecBody,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the generated text (passwords replaced by a placeholder: nothing is hashed here)."""
    _writer(conn, registry, who, node_id)
    spec = body.spec
    if isinstance(spec, builder.BasicAuth):
        placeholder = "$2y$12$" + "x" * 53
        users = [
            builder.BasicUser(username=u.username, hash=u.hash or placeholder) for u in spec.users
        ]
        spec = spec.model_copy(update={"users": users})
    return {
        "path": _path(spec.name),
        "content": builder.render(spec),
        "reference": builder.reference(spec.name),
    }


@router.post("", status_code=201, summary="Create a middleware")
async def create_middleware(
    node_id: str,
    body: builder.SpecBody,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Write `middlewares/<name>.yml`. 409 if the name is already used."""
    _writer(conn, registry, who, node_id)
    spec = await _resolved(body.spec, None)
    info = {"middleware": spec.name, "intent": spec.intent, **_size_of(body.spec)}
    try:
        snapshot = await _snapshot(request, registry, node_id)
        await _check_free(await _summaries(snapshot), spec.name, None)
        text = builder.render(spec)
        saved = await ask(
            request,
            registry,
            node_id,
            "traefik.middleware.write",
            {"name": spec.name, "content": text, "expected_sha256": None},
            schemas.Saved,
        )
        data = text.encode()
        if saved.size != len(data) or saved.sha256 != hashlib.sha256(data).hexdigest():
            raise ApiError("worker.invalid_answer", 502)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.middleware_create", node_id, info, error)
        raise
    _record(conn, registry, who, "traefik.middleware_create", node_id, **info)
    return {"name": spec.name, "reference": builder.reference(spec.name), "sha256": saved.sha256}


@router.put("/{name}", summary="Change a middleware")
async def update_middleware(
    node_id: str,
    body: builder.SpecUpdate,
    request: Request,
    name: str = MiddlewareName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Rewrite a middleware from its description. Refused if the file was edited by hand."""
    _writer(conn, registry, who, node_id)
    if body.spec.name != name:
        raise ApiError("traefik.invalid_site", 422)
    info = {"middleware": name, "intent": body.spec.intent, **_size_of(body.spec)}
    try:
        snapshot = await _snapshot(request, registry, node_id)
        current = next((f for f in snapshot.files if f.path == _path(name)), None)
        if current is None:
            raise ApiError("traefik.site_not_found", 404)
        previous = builder.untouched(current.content)
        if previous is None:
            raise ApiError("traefik.modified_by_hand", 409)
        if current.sha256 != body.expected_sha256:
            raise ApiError("worker.conflict", 409)
        if previous.intent != body.spec.intent:
            raise ApiError(
                "traefik.invalid_site", 422
            )  # another kind of middleware: make a new one
        spec = await _resolved(body.spec, previous)
        text = builder.render(spec)
        saved = await ask(
            request,
            registry,
            node_id,
            "traefik.middleware.write",
            {"name": name, "content": text, "expected_sha256": body.expected_sha256},
            schemas.Saved,
        )
        data = text.encode()
        if saved.size != len(data) or saved.sha256 != hashlib.sha256(data).hexdigest():
            raise ApiError("worker.invalid_answer", 502)
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.middleware_update", node_id, info, error)
        raise
    _record(conn, registry, who, "traefik.middleware_update", node_id, **info)
    return {"name": name, "reference": builder.reference(name), "sha256": saved.sha256}


class MiddlewareRename(BaseModel):
    """Body of a middleware rename: the new name, the version being renamed, and a dry run."""

    model_config = ConfigDict(extra="forbid")
    new_name: str = Field(pattern=builder.NAME_PATTERN)
    expected_sha256: str = Field(pattern=schemas.SHA_PATTERN)
    # Only report what the rename would change (and what it cannot), writing nothing.
    dry_run: bool = False


async def _users_of(
    request: Request, registry: Registry, node_id: str, snapshot: schemas.Snapshot, ref: str
) -> tuple[list[tuple[schemas.SiteFile, sitebuilder.SiteSpec]], list[str]]:
    """Find who names a middleware: sites the form built (changeable) and other files (not).

    A paused site is not loaded by Traefik, but it would break when resumed: it is reported
    like a hand-written file, since the form does not rewrite paused sites. A file that does
    not parse cannot be read for references, so it is reported too (it may well name this one).
    Only the file provider is looked at: a reference made from Docker labels is not seen.
    """
    listing = await ask(request, registry, node_id, "traefik.sites.list", {}, schemas.SiteFiles)
    if listing.truncated:
        raise ApiError("traefik.snapshot_incomplete", 409)
    managed: list[tuple[schemas.SiteFile, sitebuilder.SiteSpec]] = []
    managed_paths: set[str] = set()
    for site in listing.files:
        if site.paused:
            continue
        spec = sitebuilder.untouched(site.content)
        if spec is not None and ref in {parse.qualify(m) for m in spec.middlewares}:
            managed.append((site, spec))
            managed_paths.add(f"sites/{site.name}.yml")
    summaries = await _summaries(snapshot)
    summaries += await asyncio.to_thread(
        lambda: [
            parse.summarize(f"sites/{s.name}.yml.paused", s.content, s.sha256)
            for s in listing.files
            if s.paused
        ]
    )
    manual = sorted(
        {
            s["path"]
            for s in summaries
            if s["path"] not in managed_paths
            and (
                s.get("error")
                or any(ref in r["middlewares"] for r in s["routers"])
                or any(ref in m["refs"] for m in s["middlewares"])
            )
        }
    )
    return managed, manual


async def _still_there(request: Request, registry: Registry, node_id: str, path: str) -> bool:
    """Tell whether a file is still in the configuration folder (read afresh from the machine)."""
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    return any(f.path == path for f in snapshot.files)


@router.post("/{name}/rename", summary="Rename a middleware and the sites that use it")
async def rename_middleware(
    node_id: str,
    body: MiddlewareRename,
    request: Request,
    name: str = MiddlewareName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give a generated middleware a new name, following it in the sites the form built.

    Sites and files written by hand are never rewritten: they are listed (`manual`) so that
    the person fixes them. The new middleware is written first, then the sites, then the old
    file is removed, so the sites never point at nothing; if a step fails the earlier ones are
    undone (a step is recorded for undoing before its answer is checked, since the machine may
    have done it even when the answer is lost). With `dry_run` nothing is written.
    """
    _writer(conn, registry, who, node_id)
    info = {"middleware": name, "new_name": body.new_name, "dry_run": body.dry_run}
    undo: list[tuple[str, dict]] = []
    try:
        snapshot = await _snapshot(request, registry, node_id)
        current = next((f for f in snapshot.files if f.path == _path(name)), None)
        if current is None:
            raise ApiError("traefik.site_not_found", 404)
        previous = builder.untouched(current.content)
        # The file's own description must agree with its name, or the references we would
        # follow are not the ones the file really defines.
        if previous is None or previous.name != name:
            raise ApiError("traefik.modified_by_hand", 409)
        if current.sha256 != body.expected_sha256 or body.new_name == name:
            raise ApiError("worker.conflict", 409)
        summaries = await _summaries(snapshot)
        taken = {m["name"] for s in summaries for m in s["middlewares"]}
        if body.new_name in taken or any(f.path == _path(body.new_name) for f in snapshot.files):
            raise ApiError("traefik.name_taken", 409)
        managed, manual = await _users_of(
            request, registry, node_id, snapshot, builder.reference(name)
        )
        report = {
            "name": body.new_name,
            "reference": builder.reference(body.new_name),
            "sites": sorted(site.name for site, _ in managed),
            "manual": manual,
        }
        if body.dry_run:
            return report
        renamed = previous.model_copy(update={"name": body.new_name})
        text = builder.render(renamed)
        new_hash = hashlib.sha256(text.encode()).hexdigest()
        undo.append(
            ("traefik.file.delete", {"path": _path(body.new_name), "expected_sha256": new_hash})
        )
        saved = _checked(
            await ask(
                request,
                registry,
                node_id,
                "traefik.middleware.write",
                {"name": body.new_name, "content": text, "expected_sha256": None},
                schemas.Saved,
            ),
            text,
        )
        old_ref, new_ref = builder.reference(name), builder.reference(body.new_name)
        for site, spec in managed:
            changed = spec.model_copy(
                update={
                    "middlewares": [
                        new_ref if parse.qualify(m) == old_ref else m for m in spec.middlewares
                    ]
                }
            )
            site_text = sitebuilder.render(changed)
            undo.append(
                (
                    "traefik.site.write",
                    {
                        "name": site.name,
                        "content": site.content,
                        "expected_sha256": hashlib.sha256(site_text.encode()).hexdigest(),
                    },
                )
            )
            _checked(
                await ask(
                    request,
                    registry,
                    node_id,
                    "traefik.site.write",
                    {"name": site.name, "content": site_text, "expected_sha256": site.sha256},
                    schemas.Saved,
                ),
                site_text,
            )
        try:
            await ask(
                request,
                registry,
                node_id,
                "traefik.file.delete",
                {"path": _path(name), "expected_sha256": body.expected_sha256},
                schemas.Done,
            )
        except ApiError:
            # The answer may have been lost after the machine removed the file. Undoing then
            # would point the sites back at a middleware that is gone, so look first.
            if await _still_there(request, registry, node_id, _path(name)):
                raise
    except (ApiError, asyncio.CancelledError) as error:
        for op, args in reversed(undo):
            try:  # best effort: the files stay in the history either way
                model = schemas.Saved if "content" in args else schemas.Done
                await ask(request, registry, node_id, op, args, model)
            except ApiError:
                pass
        if isinstance(error, ApiError):
            audit_failure(conn, registry, who, "traefik.middleware_rename", node_id, info, error)
        raise
    _record(
        conn,
        registry,
        who,
        "traefik.middleware_rename",
        node_id,
        middleware=name,
        new_name=body.new_name,
        sites=len(managed),
        manual=len(manual),
    )
    return {**report, "sha256": saved.sha256}


def _checked(saved: schemas.Saved, text: str) -> schemas.Saved:
    """Check the worker's answer against what was sent (its answers are not trusted)."""
    data = text.encode()
    if saved.size != len(data) or saved.sha256 != hashlib.sha256(data).hexdigest():
        raise ApiError("worker.invalid_answer", 502)
    return saved


@router.delete("/{name}", status_code=204, summary="Delete a middleware")
async def delete_middleware(
    node_id: str,
    request: Request,
    name: str = MiddlewareName,
    expected_sha256: str = "",
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete a generated middleware that no router uses (its text stays in the history)."""
    _writer(conn, registry, who, node_id)
    info = {"middleware": name}
    try:
        snapshot = await _snapshot(request, registry, node_id)
        current = next((f for f in snapshot.files if f.path == _path(name)), None)
        if current is None:
            raise ApiError("traefik.site_not_found", 404)
        found = builder.untouched(current.content)
        if found is None or found.name != name:
            raise ApiError("traefik.modified_by_hand", 409)
        if current.sha256 != expected_sha256:
            raise ApiError("worker.conflict", 409)
        ref = builder.reference(name)
        routers = [r for s in await _summaries(snapshot) for r in s["routers"]]
        if any(ref in r["middlewares"] for r in routers):
            raise ApiError("traefik.middleware_in_use", 409)
        await ask(
            request,
            registry,
            node_id,
            "traefik.file.delete",
            {"path": _path(name), "expected_sha256": expected_sha256},
            schemas.Done,
        )
    except ApiError as error:
        audit_failure(conn, registry, who, "traefik.middleware_delete", node_id, info, error)
        raise
    _record(conn, registry, who, "traefik.middleware_delete", node_id, **info)
