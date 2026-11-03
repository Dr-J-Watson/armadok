"""Node API: list, create, read, update, delete.

Visibility rule: a caller sees a node only if they hold `nodes.read` for it.
Otherwise the node is "not found" (never "forbidden") so existence is not leaked.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import NotFound
from armadok.core.grants import add_grant
from armadok.core.registry import Registry
from armadok.core.rights import ALL_SCOPES, Grant, Principal
from armadok.modules.nodes import repository

router = APIRouter(prefix="/nodes", tags=["nodes"])

NodeType = Literal["network", "docker", "full"]


class NodeIn(BaseModel):
    """Fields accepted when creating a node."""

    name: str = Field(min_length=1, max_length=100)
    type: NodeType
    description: str = Field(default="", max_length=1000)


class NodePatch(BaseModel):
    """Fields accepted when updating a node; omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    type: NodeType | None = None
    description: str | None = Field(default=None, max_length=1000)


class NodeOut(BaseModel):
    """A node as returned by the API."""

    id: str
    name: str
    type: NodeType
    description: str
    owner: str
    created_at: str


def _scope(node_id: str) -> str:
    return f"node:{node_id}"


def _audit(
    conn: sqlite3.Connection, registry: Registry, who: Principal, event: str, node_id: str
) -> None:
    audit.record(
        conn, registry.audit_events(), who.actor, event, object_type="node", object_id=node_id
    )


def _visible_or_404(
    conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str
) -> dict:
    """Return the node if the caller may read it; raise `NotFound` otherwise."""
    node = repository.get_node(conn, node_id)
    if node is None or not who.has("nodes.read", registry.rights(), _scope(node_id)):
        raise NotFound()
    return node


@router.get("", response_model=list[NodeOut], summary="List the nodes you can see")
def list_nodes(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the nodes for which the caller holds `nodes.read`."""
    rights = registry.rights()
    nodes = repository.list_nodes(conn)
    return [n for n in nodes if who.has("nodes.read", rights, _scope(n["id"]))]


@router.post("", response_model=NodeOut, status_code=201, summary="Create a node")
def create_node(
    body: NodeIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a node. Requires `nodes.create` everywhere; the caller becomes its owner."""
    who.require("nodes.create", registry.rights(), ALL_SCOPES)
    node = repository.create_node(conn, body.name, body.type, body.description, who.id)
    # The creator owns the node: grant them what they need on it (and nothing elsewhere).
    for right in ("nodes.read", "nodes.update", "nodes.delete"):
        add_grant(conn, who.id, Grant(right, _scope(node["id"])), granted_by=who.id)
    _audit(conn, registry, who, "node.create", node["id"])
    return node


@router.get("/{node_id}", response_model=NodeOut, summary="Read a node")
def read_node(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return one node, or 404 if it does not exist or is not visible to the caller."""
    return _visible_or_404(conn, registry, who, node_id)


@router.patch("/{node_id}", response_model=NodeOut, summary="Update a node")
def update_node(
    node_id: str,
    body: NodePatch,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Change name, type or description. Requires `nodes.update` on this node."""
    _visible_or_404(conn, registry, who, node_id)
    who.require("nodes.update", registry.rights(), _scope(node_id))
    repository.update_node(conn, node_id, body.model_dump(exclude_none=True))
    _audit(conn, registry, who, "node.update", node_id)
    return repository.get_node(conn, node_id)  # type: ignore[return-value]


@router.delete("/{node_id}", status_code=204, summary="Delete a node (recoverable)")
def delete_node(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Soft-delete a node. Requires `nodes.delete` on this node."""
    _visible_or_404(conn, registry, who, node_id)
    who.require("nodes.delete", registry.rights(), _scope(node_id))
    repository.soft_delete_node(conn, node_id)
    _audit(conn, registry, who, "node.delete", node_id)
