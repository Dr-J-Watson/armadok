"""Builds the relations graph of a node from every module's providers.

One source feeds both the per-object "Relations" panel and the bubble map, so they can
never disagree. The viewer's rights decide which providers (and therefore which objects)
they see, and a link is shown only when both of its ends are visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import RelationLink, RelationObject
from armadok.core.registry import Registry
from armadok.core.rights import Principal


@dataclass
class Graph:
    """Objects and links of one node, plus the providers that could not answer."""

    objects: dict[tuple[str, str], RelationObject] = field(default_factory=dict)
    links: list[RelationLink] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    def neighbours(self, ref: tuple[str, str]) -> list[dict[str, Any]]:
        """Return every link touching `ref`, from either end, with the object on the other side."""
        found = []
        for link in self.links:
            if link.source == ref and link.target in self.objects:
                found.append((link, "outgoing", self.objects[link.target]))
            elif link.target == ref and link.source in self.objects:
                found.append((link, "incoming", self.objects[link.source]))
        return [
            {
                "direction": direction,
                "kind": link.kind,
                "object": {"type": obj.type, "id": obj.id, "label": obj.label},
            }
            for link, direction, obj in found
        ]


async def build_graph(
    app: Any, registry: Registry, who: Principal, node_id: str, node_type: str
) -> Graph:
    """Ask each applicable provider the viewer may use; merge the answers.

    A provider that fails (worker offline, ...) is listed in `unavailable` with its error
    code instead of failing the whole map.
    """
    catalogue = registry.rights()
    scope = f"node:{node_id}"
    graph = Graph()
    for provider in registry.relation_providers():
        if node_type not in provider.node_types or not who.has(provider.right, catalogue, scope):
            continue
        try:
            data = await provider.provide(app, node_id)
        except ApiError as error:
            graph.unavailable[provider.name] = error.code
            continue
        for obj in data.objects:
            graph.objects.setdefault((obj.type, obj.id), obj)
        graph.links.extend(data.links)
    # A link whose end the viewer cannot see must not reveal that the end exists.
    graph.links = [
        link
        for link in dict.fromkeys(graph.links)
        if link.source in graph.objects and link.target in graph.objects
    ]
    return graph
