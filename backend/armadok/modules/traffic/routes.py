"""Endpoints of the traffic statistics, per machine.

The figures from Traefik's metrics are aggregate (counts per service, error shares, response
times) and hold nothing personal; reading them needs `traffic.read`. The access-log views
(`/visitors`, `/log`) hold visitor addresses: they need `traffic.logs`, show addresses only as
networks unless the caller also holds `traffic.read_ip`, and every consultation is audited.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from fastapi import APIRouter, Depends, Query, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traffic import accesslog, accesslog_repository, repository, service
from armadok.modules.workers.access import authorise

router = APIRouter(prefix="/nodes/{node_id}/traffic", tags=["traffic"])
MIN_TEST_GAP_SECONDS = 10
_last_test: dict[str, float] = {}


@router.get("", summary="Traffic of the sites of a machine")
async def traffic(
    node_id: str,
    range: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),  # noqa: A002
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return per-service volumes, error shares, response times and a short series."""
    authorise(conn, registry, who, node_id, "traffic.read")
    data = await asyncio.to_thread(service.statistics, conn, node_id, range)
    return {**data, "status": repository.node_status(conn, node_id)}


@router.post("/test", summary="Read the metrics once to check the set-up")
async def test(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Ask the machine's worker to read Traefik's metrics now; say if it worked, and why not."""
    authorise(conn, registry, who, node_id, "traffic.read")
    now = time.monotonic()
    if now - _last_test.get(node_id, -1e9) < MIN_TEST_GAP_SECONDS:
        raise ApiError("traffic.too_soon", 429)
    _last_test[node_id] = now
    error = await service.read_node(request.app, node_id)
    return {"ok": error is None, "error": error}


Range = Query(default="24h", pattern="^(1h|24h|7d)$")
Service = Query(default=None, max_length=200)


def _consulted(
    conn, registry, who, node_id: str, what: str, with_ip: bool, by_address: bool = False
) -> None:
    """Record that logs were consulted (and whether addresses were shown), never what they held."""
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "traffic.ip_read" if with_ip else "traffic.logs_read",
        object_type="node",
        object_id=node_id,
        details={"view": what, "by_address": by_address},
    )


@router.get("/visitors", summary="Top visitors, paths and status of the access log")
async def visitors(
    node_id: str,
    range: str = Range,  # noqa: A002
    service_name: str | None = Query(default=None, alias="service", max_length=200),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the busiest visitors, the most requested paths and the status split.

    Needs `traffic.logs`. Addresses are shown as networks unless the caller holds
    `traffic.read_ip`; `masked` says which.
    """
    authorise(conn, registry, who, node_id, "traffic.logs")
    with_ip = who.has("traffic.read_ip", registry.rights(), f"node:{node_id}")
    data = await asyncio.to_thread(accesslog.visitors, conn, node_id, range, service_name, with_ip)
    _consulted(conn, registry, who, node_id, "visitors", with_ip)
    return data


@router.get("/log", summary="The newest access-log entries")
async def log_entries(
    node_id: str,
    range: str = Range,  # noqa: A002
    service_name: str | None = Query(default=None, alias="service", max_length=200),
    status_class: int | None = Query(default=None, ge=1, le=9),
    path: str | None = Query(default=None, max_length=100),
    ip: str | None = Query(default=None, max_length=45),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return up to 100 recent entries. Filtering by address needs `traffic.read_ip`."""
    authorise(conn, registry, who, node_id, "traffic.logs")
    scope = f"node:{node_id}"
    with_ip = who.has("traffic.read_ip", registry.rights(), scope)
    if ip is not None:
        who.require("traffic.read_ip", registry.rights(), scope)
    rows = await asyncio.to_thread(
        accesslog.entries,
        conn,
        node_id,
        range,
        service=service_name,
        klass=status_class,
        path=path,
        ip=ip,
        with_ip=with_ip,
    )
    _consulted(conn, registry, who, node_id, "log", with_ip, ip is not None)
    return {"masked": not with_ip, "entries": rows}


@router.delete("/log", summary="Delete the kept access-log entries of a machine")
def purge_log(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Erase every kept entry now; reading resumes from the log's end. Needs `traffic.purge`."""
    authorise(conn, registry, who, node_id, "traffic.purge")
    removed = accesslog_repository.forget_node(conn, node_id)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "traffic.purge",
        object_type="node",
        object_id=node_id,
        details={"removed": removed},
        commit=False,
    )
    conn.commit()  # the deletion and its audit entry land together
    return {"removed": removed}
