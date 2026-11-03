"""Relations endpoints: the whole map of a node, and the panel of one object."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.relations.service import Graph, build_graph

router = APIRouter(prefix="/nodes/{node_id}/relations", tags=["relations"])


async def _graph(
    request: Request, conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str
) -> Graph:
    """Return the viewer's graph of a node, or `NotFound` if they cannot see the node."""
    node = nodes_repo.get_node(conn, node_id)
    if node is None or not who.has("nodes.read", registry.rights(), f"node:{node_id}"):
        raise NotFound()
    return await build_graph(request.app, registry, who, node_id, node["type"])


@router.get("", summary="Relations map of a node")
async def node_relations(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return every visible object and link of the node, for the bubble map."""
    graph = await _graph(request, conn, registry, who, node_id)
    return {
        "objects": [{"type": o.type, "id": o.id, "label": o.label} for o in graph.objects.values()],
        "links": [
            {
                "source": {"type": link.source[0], "id": link.source[1]},
                "target": {"type": link.target[0], "id": link.target[1]},
                "kind": link.kind,
            }
            for link in graph.links
        ],
        "unavailable": graph.unavailable,
    }


@router.get("/{object_type}/{object_id:path}", summary="Relations panel of one object")
async def object_relations(
    node_id: str,
    object_type: str,
    object_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return what one object is linked to, in both directions."""
    graph = await _graph(request, conn, registry, who, node_id)
    ref = (object_type, object_id)
    if ref not in graph.objects:
        raise NotFound()
    obj = graph.objects[ref]
    return {
        "object": {"type": obj.type, "id": obj.id, "label": obj.label},
        "links": graph.neighbours(ref),
        "unavailable": graph.unavailable,
    }
