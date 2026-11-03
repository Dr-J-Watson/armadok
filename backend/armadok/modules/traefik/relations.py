"""Relations from Traefik's configuration files: routers, services, middlewares, domains.

Names follow Traefik's (`name@file`) so they meet the routers that come from Docker labels
(`name@docker`). When the node also runs Docker, a service whose server is a container name is
linked to that container.
"""

from __future__ import annotations

import asyncio
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import RelationData, RelationLink, RelationObject
from armadok.modules.docker import schemas as docker_schemas
from armadok.modules.traefik import parse, schemas
from armadok.modules.workers.routes import call_operation


async def _containers_by_name(app: Any, node_id: str) -> dict[str, str]:
    """Return `{container name: container id}`, or nothing if Docker is not reachable."""
    try:
        result = await call_operation(
            app, app.state.registry, node_id, "docker.containers.list", {}
        )
        listed = docker_schemas.ContainerList.model_validate(result).containers
    except Exception:  # noqa: BLE001  (best effort: Docker links are a bonus)
        return {}
    return {name: c.id for c in listed for name in c.names}


async def provide(app: Any, node_id: str, viewer: Any) -> RelationData:
    """Return the Traefik objects and links defined in the node's configuration files."""
    result = await call_operation(app, app.state.registry, node_id, "traefik.snapshot", {})
    try:
        snapshot = schemas.Snapshot.model_validate(result)
    except Exception:
        raise ApiError("worker.invalid_answer", 502) from None
    # Container links are a bonus drawn from Docker data: only for viewers who may see Docker.
    scope = f"node:{node_id}"
    may_see_docker = viewer.has("docker.read", app.state.registry.rights(), scope)
    containers = await _containers_by_name(app, node_id) if may_see_docker else {}

    objects: dict[tuple[str, str], RelationObject] = {}
    links: list[RelationLink] = []

    def add(kind: str, ident: str, label: str | None = None) -> tuple[str, str]:
        objects.setdefault((kind, ident), RelationObject(kind, ident, label or ident))
        return (kind, ident)

    summaries = await asyncio.to_thread(
        lambda: [parse.summarize(f.path, f.content, f.sha256) for f in snapshot.files]
    )
    for summary in summaries:
        for service in summary["services"]:
            ref = add("service", service["id"])
            for url in service["urls"]:
                # `urls` are already reduced to scheme://host[:port] by the parser.
                host = url.split("://", 1)[-1].rsplit(":", 1)[0]
                if host in containers:
                    links.append(
                        RelationLink(ref, add("container", containers[host], host), "targets")
                    )
        for middleware in summary["middlewares"]:
            add("middleware", middleware["id"])
        for router in summary["routers"]:
            ref = add("router", router["id"])
            if router["service"]:
                links.append(RelationLink(ref, add("service", router["service"]), "routes_to"))
            for name in router["middlewares"]:
                links.append(RelationLink(ref, add("middleware", name), "uses"))
            for host in parse.hosts_of(router["rule"]):
                links.append(RelationLink(ref, add("domain", host), "serves"))
    return RelationData(tuple(objects.values()), tuple(links))
