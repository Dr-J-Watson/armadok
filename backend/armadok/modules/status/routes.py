"""Endpoints of public status pages.

Managing a page needs `uptime.manage` on the probe's machine (the same sensitive right as the
probe itself). Reading one needs nothing but its address: that is the point of the page, so what
it shows is cut down to a title, up or down, and a few dots. Wrong addresses are slowed down per
caller, and a page is read from the database at most every few seconds.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.core.throttle import FailureLimiter
from armadok.modules.status import repository as repo
from armadok.modules.uptime import repository as probes_repo
from armadok.modules.workers.access import authorise

manage_router = APIRouter(prefix="/nodes/{node_id}/uptime/{probe_id}/public", tags=["status"])
public_router = APIRouter(prefix="/status", tags=["status"])

BARS = 48
CACHE_SECONDS = 15
MAX_CACHED = 500
# Wrong addresses per minute and per caller; the right address is never blocked by this.
guess_limiter = FailureLimiter({"ip": 30})
_cache: dict[str, tuple[float, dict]] = {}
ProbeId = Path(pattern=r"^[0-9a-f]{16}$")
Slug = Path(pattern=r"^[A-Za-z0-9_-]{20,40}$")


class PublishIn(BaseModel):
    """The name shown on the page: written for the public, not taken from the probe."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(pattern=r"^[^\x00-\x1f\x7f]{1,80}$")


def _probe(conn: sqlite3.Connection, node_id: str, probe_id: str):
    row = probes_repo.get(conn, probe_id)
    if row is None or row["node_id"] != node_id:
        raise NotFound()
    return row


def _audit(conn, registry, who, event, node_id, probe_id):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        details={"probe": probe_id},
    )


@manage_router.get("", summary="The public page of a probe, if there is one")
def get_page(
    node_id: str,
    probe_id: str = ProbeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the page's address and title (needs `uptime.manage`: the address is the secret)."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    _probe(conn, node_id, probe_id)
    page = repo.for_probe(conn, probe_id)
    return {
        "published": page is not None,
        "slug": page["slug"] if page else None,
        "title": page["title"] if page else None,
    }


@manage_router.put("", summary="Publish a probe on a public page")
def publish(
    node_id: str,
    body: PublishIn,
    probe_id: str = ProbeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Open (or retitle) the public page. Anyone with its address can then see the state."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    _probe(conn, node_id, probe_id)
    slug = repo.publish(conn, probe_id, body.title, who.id)
    _cache.pop(slug, None)
    _audit(conn, registry, who, "status.publish", node_id, probe_id)
    return {"published": True, "slug": slug, "title": body.title}


@manage_router.delete("", status_code=204, summary="Close the public page of a probe")
def unpublish(
    node_id: str,
    probe_id: str = ProbeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Close the page: its address stops working at once (a new page gets a new address)."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    _probe(conn, node_id, probe_id)
    page = repo.for_probe(conn, probe_id)
    if page is not None:
        _cache.pop(page["slug"], None)
    if repo.unpublish(conn, probe_id):
        _audit(conn, registry, who, "status.unpublish", node_id, probe_id)


@public_router.get("/{slug}", summary="A public status page (no sign-in)")
def read_page(
    request: Request,
    response: Response,
    slug: str = Slug,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return the title, the state (`up`, `down` or `unknown`) and the latest results."""
    caller = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cached = _cache.get(slug)
    if cached is None or now - cached[0] > CACHE_SECONDS:
        data = repo.public_view(conn, slug, BARS)
        if data is None:
            if guess_limiter.blocked(ip=caller):
                raise ApiError("status.too_many", 429)
            guess_limiter.remember(ip=caller)
            raise NotFound()
        if len(_cache) >= MAX_CACHED:
            _cache.clear()
        _cache[slug] = (now, data)
        cached = _cache[slug]
    # Nothing here is for a search engine or a shared cache to keep for long.
    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return cached[1]
