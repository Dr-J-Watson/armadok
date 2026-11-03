"""Helpers shared by every module that asks a node's worker to do something.

One place for the three things each such route needs, so they cannot drift apart:
checking that the caller may see the node and holds a right on it (404 before 403),
asking the worker and validating its (untrusted) answer, and auditing a failed attempt.
"""

from __future__ import annotations

import sqlite3

from fastapi import Request
from pydantic import BaseModel, ValidationError

from armadok.core import audit
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.workers.routes import call_operation


def authorise(
    conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str, right: str
) -> dict:
    """Return the node if the caller can see it and holds `right` on it.

    An invisible or missing node is `NotFound`, never "forbidden", so existence is not leaked.
    """
    scope = f"node:{node_id}"
    catalogue = registry.rights()
    node = nodes_repo.get_node(conn, node_id)
    if node is None or not who.has("nodes.read", catalogue, scope):
        raise NotFound()
    who.require(right, catalogue, scope)
    return node


async def ask(
    request: Request,
    registry: Registry,
    node_id: str,
    op: str,
    args: dict,
    model: type[BaseModel],
):
    """Run an operation on the node's worker and validate the answer against `model`."""
    # Only a person's own session may use what an emergency session opens up.
    session = getattr(request.state, "actor_kind", "") == "session"
    actor = getattr(request.state, "actor_id", None) if session else None
    result = await call_operation(request.app, registry, node_id, op, args, actor)
    try:
        return model.model_validate(result)
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None


def audit_failure(
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    event: str,
    node_id: str,
    details: dict[str, str | int | bool | None],
    error: ApiError,
) -> None:
    """Record a refused or failed attempt (with its error code, never any content).

    A write that times out may still have happened on the machine, and a symlink refusal is a
    tamper signal: both deserve a trace even though the caller only got an error.
    """
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        outcome="failure",
        details={**details, "code": error.code},
    )
