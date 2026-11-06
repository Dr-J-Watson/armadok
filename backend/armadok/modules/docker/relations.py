"""Relations the Docker module knows about: containers and what they touch.

Objects: container, image, network, volume, and, from the Traefik labels on containers, router,
domain and middleware. A Compose group is a `project` when its folder is one Armadok manages (a
folder of the node's projects directory) and a `compose` group otherwise: a container is not a
project just because Docker labels it, it becomes one when its folder is imported. Links are
stored once and read both ways.
"""

from __future__ import annotations

import re
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import RelationData, RelationLink, RelationObject
from armadok.modules.docker import schemas
from armadok.modules.projects import states
from armadok.modules.workers.routes import call_operation

_ROUTER_RULE = re.compile(r"traefik\.http\.routers\.([A-Za-z0-9_.-]+)\.rule")
_ROUTER_MIDDLEWARES = re.compile(r"traefik\.http\.routers\.([A-Za-z0-9_.-]+)\.middlewares")
_HOST = re.compile(r"Host\(`([^`]+)`\)")
MAX_OBJECTS = 5000


async def provide(app: Any, node_id: str, viewer: Any) -> RelationData:
    """Return the Docker objects and links of a node, from its worker's container list."""
    result = await call_operation(app, app.state.registry, node_id, "docker.containers.list", {})
    try:
        containers = schemas.ContainerList.model_validate(result).containers
    except Exception:
        raise ApiError("worker.invalid_answer", 502) from None

    managed = _managed_projects(app, node_id)
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
            kind = "project" if project in managed else "compose"
            links.append(RelationLink(container, add(kind, project), "part_of"))
        for network in c.networks:
            links.append(RelationLink(container, add("network", network), "attached_to"))
        for mount in c.mounts:
            if mount.type == "volume" and mount.name:
                links.append(RelationLink(container, add("volume", mount.name), "mounts"))
        for key, value in c.labels.items():
            if match := _ROUTER_RULE.fullmatch(key):
                router = add("router", f"{match.group(1)}@docker")
                links.append(RelationLink(router, container, "routes_to"))
                for host in _HOST.findall(value):
                    links.append(RelationLink(router, add("domain", host), "serves"))
            elif match := _ROUTER_MIDDLEWARES.fullmatch(key):
                router = add("router", f"{match.group(1)}@docker")
                for name in filter(None, (v.strip() for v in value.split(","))):
                    # A name without a provider belongs to the same one as the router.
                    full = name if "@" in name else f"{name}@docker"
                    links.append(RelationLink(router, add("middleware", full), "uses"))
    # A managed project with nothing running is still on the map.
    for name in sorted(managed):
        add("project", name)
    return RelationData(tuple(objects.values()), tuple(links))


def _managed_projects(app: Any, node_id: str) -> set[str]:
    """Names of the projects Armadok manages on the node (imported or created here)."""
    conn = app.state.settings.open_db()
    try:
        return states.managed_names(conn, node_id)
    finally:
        conn.close()
