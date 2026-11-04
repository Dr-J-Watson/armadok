"""Pause or resume every site of a domain at once, without touching the DNS.

A site is paused by renaming its file (see the site endpoints); this applies that to each site
whose host names all lie under the domain. A site that also answers for a host outside the
domain is left alone and reported (`shared`): pausing it would silence another domain too.
Sites are found from their routers' rules, so ones edited by hand count as well.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traefik import parse, schemas
from armadok.modules.traefik.sites_routes import _record, _run, _sites
from armadok.modules.workers.access import ask, authorise

router = APIRouter(prefix="/nodes/{node_id}/traefik/domains", tags=["traefik"])

Domain = Path(pattern=r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", max_length=253)


class DomainSwitch(BaseModel):
    """Body of a domain pause or resume: a dry run only reports what would change."""

    model_config = ConfigDict(extra="forbid")
    dry_run: bool = False


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


async def _plan(
    request: Request, registry: Registry, node_id: str, domain: str, paused: bool
) -> tuple[list[str], list[str]]:
    """Return `(sites to switch, sites left alone because they also serve another domain)`."""
    listing = await _sites(request, registry, node_id)
    if len(listing) >= 200:
        raise ApiError("traefik.snapshot_incomplete", 409)
    names: list[str] = []
    shared: list[str] = []
    for site in listing:
        if site.paused != paused:
            continue
        summary = await asyncio.to_thread(
            parse.summarize, f"sites/{site.name}.yml", site.content, site.sha256
        )
        hosts = {h.lower() for r in summary["routers"] for h in parse.hosts_of(r["rule"])}
        if not any(_under(h, domain) for h in hosts):
            continue
        (names if all(_under(h, domain) for h in hosts) else shared).append(site.name)
    return sorted(names), sorted(shared)


async def _switch_domain(
    action: Literal["pause", "resume"],
    node_id: str,
    domain: str,
    body: DomainSwitch,
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
) -> dict:
    authorise(conn, registry, who, node_id, "traefik.sites.write")
    event = f"traefik.domain_{action}"
    info = {"domain": domain, "dry_run": body.dry_run}

    async def call() -> dict:
        names, shared = await _plan(request, registry, node_id, domain, action == "resume")
        if body.dry_run:
            return {"sites": names, "shared": shared}
        done: list[str] = []
        undo = "traefik.site.pause" if action == "resume" else "traefik.site.resume"
        try:
            for name in names:
                await ask(
                    request,
                    registry,
                    node_id,
                    f"traefik.site.{action}",
                    {"name": name},
                    schemas.Done,
                )
                done.append(name)
        except ApiError:
            for name in reversed(done):  # all or nothing, as far as the machine lets us
                try:
                    await ask(request, registry, node_id, undo, {"name": name}, schemas.Done)
                except ApiError:
                    pass
            raise
        return {"sites": names, "shared": shared}

    result = await _run(conn, registry, who, event, node_id, info, call)
    if not body.dry_run:
        _record(conn, registry, who, event, node_id, domain=domain, sites=len(result["sites"]))
    return result


@router.post("/{domain}/pause", summary="Pause every site of a domain")
async def pause_domain(
    node_id: str,
    body: DomainSwitch,
    request: Request,
    domain: str = Domain,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Stop serving the sites whose hosts all lie under `domain`. The DNS is not touched."""
    return await _switch_domain("pause", node_id, domain, body, request, conn, registry, who)


@router.post("/{domain}/resume", summary="Serve the paused sites of a domain again")
async def resume_domain(
    node_id: str,
    body: DomainSwitch,
    request: Request,
    domain: str = Domain,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Serve again the paused sites whose hosts all lie under `domain`."""
    return await _switch_domain("resume", node_id, domain, body, request, conn, registry, who)
