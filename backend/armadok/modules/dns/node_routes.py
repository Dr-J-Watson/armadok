"""DNS endpoints scoped to a node: the report of hosts, and the one-click fix."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.dns import report
from armadok.modules.dns.base import DOMAIN_PATTERN
from armadok.modules.dns.routes import change_record
from armadok.modules.workers.access import authorise

router = APIRouter(prefix="/nodes/{node_id}/dns", tags=["dns"])


class FixIn(BaseModel):
    """A host to create the missing record for; the target defaults to the zone's apex address."""

    host: str = Field(pattern=DOMAIN_PATTERN, max_length=253)
    type: str | None = Field(default=None, pattern="^(A|AAAA|CNAME)$")
    target: str | None = Field(default=None, max_length=2000, pattern=r"^[^\x00-\x1f\x7f]+$")


@router.get("/report", summary="Hosts used by the sites of a node, and their DNS state")
async def dns_report(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return, for each host the sites use: the domain it belongs to and whether the DNS has it."""
    who.require("dns.use", registry.rights())
    node = authorise(conn, registry, who, node_id, "dns.read")
    return {"hosts": await report.node_report(request.app, registry, conn, who, node)}


@router.post("/fix", status_code=201, summary="Create the missing DNS record of a host")
async def fix(
    node_id: str,
    body: FixIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create the record a host needs, on the domain that holds it.

    Goes through the same checks and audit as creating a record by hand, including the limits
    on what a node member may write with the owner's provider account.
    """
    who.require("dns.use", registry.rights())
    node = authorise(conn, registry, who, node_id, "dns.read")
    domain, record = await report.plan_missing(
        request.app, registry, conn, who, node, body.host, body.type, body.target
    )
    created = await change_record(
        request, conn, registry, who, domain["id"], "dns.record_create", new=record
    )
    return {"id": created.id, "type": created.type, "name": created.name, "target": created.target}
