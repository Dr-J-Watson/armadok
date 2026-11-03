"""Docker endpoints of a node: what runs there, and actions on containers."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Path, Query, Request

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.docker import schemas
from armadok.modules.workers.access import ask, audit_failure, authorise

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


def _listing(path: str, op: str, model: type) -> None:
    """Register `GET /nodes/{id}/docker/<path>` returning the worker's list, validated."""

    async def handler(
        node_id: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_db),
        registry: Registry = Depends(get_registry),
        who: Principal = Depends(get_principal),
    ) -> dict:
        authorise(conn, registry, who, node_id, "docker.read")
        return (await ask(request, registry, node_id, op, {}, model)).model_dump()

    handler.__doc__ = f"Return the {path} of the machine. Needs `docker.read`."
    router.add_api_route(
        f"/{path}", handler, methods=["GET"], summary=f"{path.title()} of the machine"
    )


_listing("images", "docker.images.list", schemas.Images)
_listing("networks", "docker.networks.list", schemas.Networks)
_listing("volumes", "docker.volumes.list", schemas.Volumes)


def _gate(
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    node_id: str,
    right: str,
    event: str,
    details: dict,
    check_node: bool = True,
) -> None:
    """Require `right` on the node; a missing right is audited as `denied` before it is raised.

    A node the caller cannot see stays "not found" and leaves no trace (nothing is leaked).
    """
    try:
        if check_node:
            authorise(conn, registry, who, node_id, right)
        else:
            who.require(right, registry.rights(), f"node:{node_id}")
    except MissingRight:
        audit.record(
            conn,
            registry.audit_events(),
            who.actor,
            event,
            object_type="node",
            object_id=node_id,
            outcome="denied",
            details={**details, "right": right},
        )
        raise


Ident = Path(pattern=r"^[0-9a-f]{12,64}$", description="Container id (hex)")


@router.post("/containers/{container}/action", summary="Start, stop, restart or remove a container")
async def container_action(
    node_id: str,
    body: schemas.ActionIn,
    request: Request,
    container: str = Ident,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Run an action on one container of the node.

    Needs `docker.control`; `remove` needs `docker.remove` too (volumes are never removed).
    Every attempt is audited, with the container id and the action.
    """
    details = {"container": container, "action": body.action}
    event = "docker.container_action"
    _gate(conn, registry, who, node_id, "docker.control", event, details)
    if body.action == "remove":
        _gate(conn, registry, who, node_id, "docker.remove", event, details, check_node=False)
    try:
        await ask(
            request,
            registry,
            node_id,
            "docker.container.action",
            {"id": container, "action": body.action},
            schemas.Done,
        )
    except ApiError as error:
        audit_failure(conn, registry, who, "docker.container_action", node_id, details, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "docker.container_action",
        object_type="node",
        object_id=node_id,
        details=details,
    )
    return {"done": True}


@router.get("/containers/{container}/logs", summary="Last lines of a container's logs")
async def container_logs(
    node_id: str,
    request: Request,
    container: str = Ident,
    tail: int = Query(default=200, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the last `tail` lines. Needs `docker.logs`; the read is audited (not the content)."""
    details = {"container": container, "tail": tail}
    _gate(conn, registry, who, node_id, "docker.logs", "docker.logs_read", details)
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "docker.container.logs",
            {"id": container, "tail": tail},
            schemas.Logs,
        )
    except ApiError as error:
        audit_failure(conn, registry, who, "docker.logs_read", node_id, details, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "docker.logs_read",
        object_type="node",
        object_id=node_id,
        details=details,
    )
    return answer.model_dump()
