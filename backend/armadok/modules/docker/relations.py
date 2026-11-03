"""Relations the Docker module knows about: containers and what they touch.

Objects: container, image, project (Compose), network, volume, and, from the Traefik labels
on containers, router, domain and middleware. Links are stored once and read both ways.
"""

from __future__ import annotations

import re
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import RelationData, RelationLink, RelationObject
from armadok.modules.docker import schemas
from armadok.modules.workers.routes import call_operation

_ROUTER_RULE = re.compile(r"traefik\.http\.routers\.([A-Za-z0-9_.-]+)\.rule")
_ROUTER_MIDDLEWARES = re.compile(r"traefik\.http\.routers\.([A-Za-z0-9_.-]+)\.middlewares")
_HOST = re.compile(r"Host\(`([^`]+)`\)")
MAX_OBJECTS = 5000


async def provide(app: Any, node_id: str) -> RelationData:
    """Return the Docker objects and links of a node, from its worker's container list."""
    result = await call_operation(app, app.state.registry, node_id, "docker.containers.list", {})
    try:
        containers = schemas.ContainerList.model_validate(result).containers
    except Exception:
        raise ApiError("worker.invalid_answer", 502) from None

    objects: dict[tuple[str, str], RelationObject] = {}
    links: list[RelationLink] = []

    def add(kind: str, ident: str, label: str | None = None) -> tuple[str, str]:
        if len(objects) < MAX_OBJECTS:
            objects.setdefault((kind, ident), RelationObject(kind, ident, label or ident))
        return (kind, ident)

    for c in containers:
        container = add("container", c.id, c.names[0] if c.names else c.id[:12])
        links.append(RelationLink(container, add("image", c.image), "runs"))
        if project := c.labels.get("com.docker.compose.project"):
            links.append(RelationLink(container, add("project", project), "part_of"))
        for network in c.networks:
            links.append(RelationLink(container, add("network", network), "attached_to"))
        for mount in c.mounts:
            if mount.type == "volume" and mount.name:
                links.append(RelationLink(container, add("volume", mount.name), "mounts"))
        for key, value in c.labels.items():
            if match := _ROUTER_RULE.fullmatch(key):
                router = add("router", match.group(1))
                links.append(RelationLink(router, container, "routes_to"))
                for host in _HOST.findall(value):
                    links.append(RelationLink(router, add("domain", host), "serves"))
            elif match := _ROUTER_MIDDLEWARES.fullmatch(key):
                router = add("router", match.group(1))
                for name in filter(None, (v.strip() for v in value.split(","))):
                    links.append(RelationLink(router, add("middleware", name), "uses"))
    return RelationData(tuple(objects.values()), tuple(links))
