"""Endpoints of the traffic statistics, per machine.

Everything here is aggregate: counts per service, error shares, response times. Traefik's
metrics never carry a visitor's address, so nothing personal is held. Reading needs
`traffic.read`; the test that asks the worker to read the metrics once needs it too and is paced.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from fastapi import APIRouter, Depends, Query, Request

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.traffic import repository, service
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
