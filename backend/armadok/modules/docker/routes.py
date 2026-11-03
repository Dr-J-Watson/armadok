"""Docker endpoints of a node: version and containers (read-only for now)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.docker import schemas
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.workers.routes import call_operation

router = APIRouter(prefix="/nodes/{node_id}/docker", tags=["docker"])


def _authorise(conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str) -> None:
    """Allow only callers who can see the node and hold `docker.read` on it."""
    scope = f"node:{node_id}"
    catalogue = registry.rights()
    if nodes_repo.get_node(conn, node_id) is None or not who.has("nodes.read", catalogue, scope):
        raise NotFound()
    who.require("docker.read", catalogue, scope)


async def _ask(request: Request, registry: Registry, node_id: str, op: str, model):
    """Run an operation on the node's worker and validate its (untrusted) answer."""
    result = await call_operation(request.app, registry, node_id, op, {})
    try:
        return model.model_validate(result)
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None


@router.get("/info", summary="Docker version of the machine")
async def docker_info(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the Docker and API versions reported by the node's worker."""
    _authorise(conn, registry, who, node_id)
    info = await _ask(request, registry, node_id, "docker.info", schemas.DockerInfo)
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
    _authorise(conn, registry, who, node_id)
    answer = await _ask(request, registry, node_id, "docker.containers.list", schemas.ContainerList)
    return answer.model_dump()
