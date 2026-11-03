"""Certificate endpoint of a node: what Traefik obtained, and which sites lack a certificate."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.certs import schemas, service
from armadok.modules.relations.service import build_graph
from armadok.modules.workers.access import ask, authorise

router = APIRouter(prefix="/nodes/{node_id}/certs", tags=["certs"])


@router.get("", summary="Certificates of a node and the sites that lack one")
async def node_certs(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the certificates Traefik obtained and the hosts the sites use, with their state.

    Without `certs.read_all`, only certificates that cover a host the caller can see are shown:
    an editor learns about their own domains, not about everyone's. Private keys never reach the
    hub: the worker only sends public facts.
    """
    node = authorise(conn, registry, who, node_id, "certs.read")
    answer = await ask(request, registry, node_id, "certs.list", {}, schemas.CertificateList)
    graph = await build_graph(request.app, registry, who, node_id, node["type"])
    hosts = sorted({o.id for o in graph.objects.values() if o.type == "domain"})
    see_all = who.has("certs.read_all", registry.rights(), f"node:{node_id}")
    certs = answer.certificates
    now = datetime.now(UTC)
    if see_all:
        shown = [service.describe(c, now) for c in certs]
    else:
        # Only certificates that cover a host the viewer sees, and of those only the names that
        # cover such a host: a multi-domain certificate must not reveal the other names.
        shown = []
        for c in certs:
            visible = [n for n in c.names if any(service.covers(n, h) for h in hosts)]
            if visible:
                shown.append(service.describe(c, now, visible))
    report = []
    for host in hosts:
        cover = service.best_cover(host, certs)
        report.append(
            {
                "host": host,
                # The serial would let a viewer tell that two of their hosts share a certificate
                # with names they cannot see.
                "serial": cover.serial if cover and see_all else None,
                "state": service.state(service.days_left(cover, now)) if cover else "missing",
            }
        )
    return {
        "certificates": shown,
        "hosts": report,
        "truncated": answer.truncated,
    }
