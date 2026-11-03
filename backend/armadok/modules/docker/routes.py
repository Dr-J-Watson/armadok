"""Docker endpoints of a node: version and containers (read-only for now)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.docker import schemas
from armadok.modules.workers.access import ask, authorise

router = APIRouter(prefix="/nodes/{node_id}/docker", tags=["docker"])


@router.get("/info", summary="Docker version of the machine")
async def docker_info(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the Docker and API versions reported by the node's worker."""
    authorise(conn, registry, who, node_id, "docker.read")
    info = await ask(request, registry, node_id, "docker.info", {}, schemas.DockerInfo)
    return info.model_dump()


@router.get("/containers", summary="Containers of the machine")
async def list_containers(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return all containers with their state, ports, networks and mounts."""
    authorise(conn, registry, who, node_id, "docker.read")
    answer = await ask(
        request, registry, node_id, "docker.containers.list", {}, schemas.ContainerList
    )
    return answer.model_dump()
