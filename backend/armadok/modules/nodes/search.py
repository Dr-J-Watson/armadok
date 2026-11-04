"""Search provider of the nodes module: the machines the caller can see."""

from __future__ import annotations

from armadok.core.module import SearchContext, SearchHit
from armadok.modules.nodes import repository


def run(ctx: SearchContext, text: str, limit: int) -> list[SearchHit]:
    """Return the machines whose name contains `text` and that the caller may read."""
    catalogue = ctx.registry.rights()
    hits = []
    for node in repository.list_nodes(ctx.conn):
        # Like the node list: a suspended machine is still found by those who may see it.
        if text in node["name"].lower() and ctx.who.has(
            "nodes.read", catalogue, f"node:{node['id']}", ignore_blocked=True
        ):
            hits.append(SearchHit("node", node["name"], f"/nodes/{node['id']}", node["type"]))
            if len(hits) >= limit:
                break
    return hits
