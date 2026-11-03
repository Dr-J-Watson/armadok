"""Site endpoints of a node: build, change, pause, rename and delete sites; file history.

A site is one generated file in `sites/` (see `sitebuilder`). Needs `traefik.sites.write` to
change anything; history and restore work on any configuration file and follow the file rights.
Every change is audited by name, never by content.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traefik import parse, schemas, sitebuilder
from armadok.modules.traefik.routes import _checked_path, require_for_sites_folder
from armadok.modules.workers.access import ask, audit_failure, authorise

router = APIRouter(prefix="/nodes/{node_id}/traefik", tags=["traefik"])

Name = Path(pattern=sitebuilder.SITE_NAME_PATTERN, description="Site name")


async def _sites(request: Request, registry: Registry, node_id: str) -> list[schemas.SiteFile]:
    answer = await ask(request, registry, node_id, "traefik.sites.list", {}, schemas.SiteFiles)
    return answer.files


async def _undo_create(
    request: Request, registry: Registry, node_id: str, name: str, sha256: str
) -> None:
    """Remove a site file we just created (best effort; its text stays in the history)."""
    try:
        await ask(
            request,
            registry,
            node_id,
            "traefik.site.delete",
            {"name": name, "expected_sha256": sha256},
            schemas.Done,
        )
    except ApiError:
        pass


def _verified(saved: schemas.Saved, text: str) -> schemas.Saved:
    """Check the worker's answer against what was sent (its answers are not trusted)."""
    data = text.encode()
    if saved.size != len(data) or saved.sha256 != hashlib.sha256(data).hexdigest():
        raise ApiError("worker.invalid_answer", 502)
    return saved


async def _one(
    request: Request, registry: Registry, node_id: str, name: str
) -> tuple[schemas.SiteFile, sitebuilder.SiteSpec]:
    """Return a site file that the form generated and nobody edited, or refuse."""
    found = next((f for f in await _sites(request, registry, node_id) if f.name == name), None)
    if found is None:
        raise ApiError("traefik.site_not_found", 404)
    spec = sitebuilder.untouched(found.content)
    if spec is None:
        raise ApiError("traefik.modified_by_hand", 409)
    return found, spec


async def _check_free(
    request: Request, registry: Registry, node_id: str, name: str, own_path: str | None
) -> None:
    """Refuse a name already used by a router or a service of another file."""
    snapshot = await ask(request, registry, node_id, "traefik.snapshot", {}, schemas.Snapshot)
    listing = await ask(request, registry, node_id, "traefik.sites.list", {}, schemas.SiteFiles)
    if snapshot.truncated or listing.truncated:
        # Names we could not see might collide: better to refuse than to guess.
        raise ApiError("traefik.snapshot_incomplete", 409)
    # Paused sites are not loaded by Traefik, but resuming them must not collide either.
    paused = [f for f in listing.files if f.paused]
    summaries = await asyncio.to_thread(
        lambda: (
            [parse.summarize(f.path, f.content, f.sha256) for f in snapshot.files]
            + [parse.summarize(f"sites/{f.name}.yml.paused", f.content, f.sha256) for f in paused]
        )
    )
    for summary in summaries:
        if summary["path"] == own_path:
            continue
        taken = {r["name"] for r in summary["routers"]} | {s["name"] for s in summary["services"]}
        if name in taken:
            raise ApiError("traefik.name_taken", 409)
    if any(f.name == name for f in listing.files) and own_path is None:
        raise ApiError("traefik.name_taken", 409)


def _record(
    conn: sqlite3.Connection, registry: Registry, who: Principal, event: str, node_id: str, **info
) -> None:
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        details=info,
    )


async def _run(
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    event: str,
    node_id: str,
    info: dict,
    call: Callable[[], Awaitable[Any]],
) -> Any:
    """Run `call`; a failure is audited with its code before it is raised again."""
    try:
        return await call()
    except ApiError as error:
        audit_failure(conn, registry, who, event, node_id, info, error)
        raise


@router.get("/managed", summary="Sites built with the form")
async def managed(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the sites of the `sites/` folder: description, paused or not, edited by hand."""
    authorise(conn, registry, who, node_id, "traefik.read")
    out = []
    for f in await _sites(request, registry, node_id):
        spec = sitebuilder.spec_of(f.content)
        out.append(
            {
                "name": f.name,
                "paused": f.paused,
                "sha256": f.sha256,
                "spec": spec.model_dump() if spec else None,
                "modified_by_hand": spec is None or sitebuilder.untouched(f.content) is None,
            }
        )
    return {"sites": out}


@router.post("/sites/preview", summary="Show the file a site description would produce")
async def preview(
    node_id: str,
    body: schemas.SiteBody,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the generated text and say whether the name is free. Writes nothing."""
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    taken = False
    try:
        await _check_free(request, registry, node_id, body.spec.name, None)
    except ApiError as error:
        if error.code != "traefik.name_taken":
            raise
        taken = True
    return {"yaml": sitebuilder.render(body.spec), "name_taken": taken}


@router.post("/sites", status_code=201, summary="Create a site")
async def create_site(
    node_id: str,
    body: schemas.SiteBody,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Write `sites/<name>.yml`. 409 if the name is already used by a router or a service."""
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    name, info = body.spec.name, {"site": body.spec.name}

    async def call() -> dict:
        await _check_free(request, registry, node_id, name, None)
        text = sitebuilder.render(body.spec)
        saved = await ask(
            request,
            registry,
            node_id,
            "traefik.site.write",
            {"name": name, "content": text, "expected_sha256": None},
            schemas.Saved,
        )
        return {"name": name, "sha256": _verified(saved, text).sha256}

    result = await _run(conn, registry, who, "traefik.site_create", node_id, info, call)
    _record(conn, registry, who, "traefik.site_create", node_id, **info)
    return result


@router.put("/sites/{name}", summary="Change a site")
async def update_site(
    node_id: str,
    body: schemas.SiteUpdate,
    request: Request,
    name: str = Name,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Rewrite a site from its description. Refused if the file was edited by hand or paused."""
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    info = {"site": name}
    if body.spec.name != name:
        raise ApiError("traefik.invalid_site", 422)

    async def call() -> dict:
        found, _ = await _one(request, registry, node_id, name)
        if found.paused:
            raise ApiError("traefik.site_paused", 409)
        if found.sha256 != body.expected_sha256:
            raise ApiError("worker.conflict", 409)
        text = sitebuilder.render(body.spec)
        saved = await ask(
            request,
            registry,
            node_id,
            "traefik.site.write",
            {"name": name, "content": text, "expected_sha256": body.expected_sha256},
            schemas.Saved,
        )
        return {"name": name, "sha256": _verified(saved, text).sha256}

    result = await _run(conn, registry, who, "traefik.site_update", node_id, info, call)
    _record(conn, registry, who, "traefik.site_update", node_id, **info)
    return result


async def _switch(
    op: str,
    event: str,
    node_id: str,
    name: str,
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
) -> dict:
    """Pause or resume: the same checks, audit and answer for both."""
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    info = {"site": name}

    async def call() -> dict:
        await ask(request, registry, node_id, op, {"name": name}, schemas.Done)
        return {"done": True}

    result = await _run(conn, registry, who, event, node_id, info, call)
    _record(conn, registry, who, event, node_id, **info)
    return result


@router.post("/sites/{name}/pause", summary="Stop serving a site, keeping its file")
async def pause_site(
    node_id: str,
    request: Request,
    name: str = Name,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Rename the site's file so that Traefik ignores it. Nothing is deleted."""
    return await _switch(
        "traefik.site.pause", "traefik.site_pause", node_id, name, request, conn, registry, who
    )


@router.post("/sites/{name}/resume", summary="Serve a paused site again")
async def resume_site(
    node_id: str,
    request: Request,
    name: str = Name,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give the paused site's file its name back, unchanged."""
    return await _switch(
        "traefik.site.resume", "traefik.site_resume", node_id, name, request, conn, registry, who
    )


@router.post("/sites/{name}/rename", summary="Rename a site")
async def rename_site(
    node_id: str,
    body: schemas.SiteRename,
    request: Request,
    name: str = Name,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create the site under its new name, then remove the old file (kept in the history)."""
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    info = {"site": name, "new_name": body.new_name}

    async def call() -> dict:
        found, spec = await _one(request, registry, node_id, name)
        if found.paused:
            raise ApiError("traefik.site_paused", 409)
        if found.sha256 != body.expected_sha256 or body.new_name == name:
            raise ApiError("worker.conflict", 409)
        await _check_free(request, registry, node_id, body.new_name, None)
        renamed = spec.model_copy(update={"name": body.new_name})
        text = sitebuilder.render(renamed)
        saved = _verified(
            await ask(
                request,
                registry,
                node_id,
                "traefik.site.write",
                {"name": body.new_name, "content": text, "expected_sha256": None},
                schemas.Saved,
            ),
            text,
        )
        try:
            await ask(
                request,
                registry,
                node_id,
                "traefik.site.delete",
                {"name": name, "expected_sha256": found.sha256},
                schemas.Done,
            )
        except ApiError:
            # The old file stays: take the new one back, so no two sites share one rule.
            await _undo_create(request, registry, node_id, body.new_name, saved.sha256)
            raise
        return {"name": body.new_name, "sha256": saved.sha256}

    result = await _run(conn, registry, who, "traefik.site_rename", node_id, info, call)
    _record(conn, registry, who, "traefik.site_rename", node_id, **info)
    return result


@router.delete("/sites/{name}", summary="Delete a site")
async def delete_site(
    node_id: str,
    request: Request,
    expected_sha256: str = Query(pattern=schemas.SHA_PATTERN),
    name: str = Name,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Remove a site's file (active or paused); its last version stays in the history."""
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    info = {"site": name}

    async def call() -> dict:
        await ask(
            request,
            registry,
            node_id,
            "traefik.site.delete",
            {"name": name, "expected_sha256": expected_sha256},
            schemas.Done,
        )
        return {"done": True}

    result = await _run(conn, registry, who, "traefik.site_delete", node_id, info, call)
    _record(conn, registry, who, "traefik.site_delete", node_id, **info)
    return result


@router.get("/history/{path:path}", summary="Kept versions of a configuration file")
async def history(
    node_id: str,
    path: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the versions kept before each change (newest first). Needs `traefik.files.read`."""
    authorise(conn, registry, who, node_id, "traefik.files.read")
    _checked_path(path)
    answer = await ask(
        request, registry, node_id, "traefik.history.list", {"path": path}, schemas.Versions
    )
    return answer.model_dump()


@router.post("/restore/{path:path}", summary="Put back a kept version of a file")
async def restore(
    node_id: str,
    path: str,
    body: schemas.RestoreIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Write a kept version back (the current text becomes a version itself)."""
    authorise(conn, registry, who, node_id, "traefik.files.write")
    _checked_path(path)
    require_for_sites_folder(who, registry, node_id, path)
    info = {"path": path, "version": body.version}

    async def call() -> dict:
        old = await ask(
            request,
            registry,
            node_id,
            "traefik.history.read",
            {"path": path, "version": body.version},
            schemas.FileContent,
        )
        try:
            await asyncio.to_thread(parse.load, old.content)
        except parse.YamlError:
            raise ApiError("traefik.invalid_yaml", 422) from None
        saved = await ask(
            request,
            registry,
            node_id,
            "traefik.file.write",
            {"path": path, "content": old.content, "expected_sha256": body.expected_sha256},
            schemas.Saved,
        )
        if saved.sha256 != hashlib.sha256(old.content.encode()).hexdigest():
            raise ApiError("worker.invalid_answer", 502)
        return saved.model_dump()

    result = await _run(conn, registry, who, "traefik.history_restore", node_id, info, call)
    _record(conn, registry, who, "traefik.history_restore", node_id, **info)
    return result
