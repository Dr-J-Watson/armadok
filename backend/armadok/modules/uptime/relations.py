"""Relations of the availability probes: a probe is linked to the domain it watches.

The domain object itself comes from the Traefik provider; when no site serves the probed host,
the link is simply not drawn (the relations service drops links to unknown objects). So a probe
shows up next to the site it watches, and the site shows its probe, state included.
"""

from __future__ import annotations

import asyncio
from typing import Any

from armadok.core.module import RelationData, RelationLink, RelationObject
from armadok.modules.uptime import repository


async def provide(app: Any, node_id: str, viewer: Any) -> RelationData:
    """Return the node's probes and the links to the domains they watch."""

    def load() -> list[dict[str, Any]]:
        conn = app.state.settings.open_db()
        try:
            return [dict(r) for r in repository.list_for_node(conn, node_id)]
        finally:
            conn.close()

    objects: list[RelationObject] = []
    links: list[RelationLink] = []
    for probe in await asyncio.to_thread(load):
        # The state is part of the label so the map shows at a glance what is down.
        ref = ("probe", probe["id"])
        objects.append(RelationObject("probe", probe["id"], f"{probe['name']} ({probe['state']})"))
        if probe["kind"] == "http":
            links.append(RelationLink(ref, ("domain", probe["host"].lower()), "watches"))
    return RelationData(tuple(objects), tuple(links))
