"""The closed list of operations a worker can run.

Adding an operation = write a function in a module under `armadok/worker/`, add it to that
module's `OPERATIONS`, include the module below, and declare the same name (with its node
types) in the owning hub module's `worker_ops`. Anything not listed is refused.
"""

from __future__ import annotations

from typing import Any

from armadok.worker import (
    accesslog_ops,
    backup_ops,
    certs_ops,
    compose_ops,
    docker_control,
    docker_ops,
    git_ops,
    image_ops,
    inspect_ops,
    nodeconfig,
    projects_ops,
    sites_ops,
    stats_ops,
    terminal_ops,
    traefik_ops,
    traffic_ops,
    uptime_ops,
)
from armadok.worker.operation import Operation, OperationError

__all__ = ["OPERATIONS", "Operation", "OperationError"]


def ping(args: dict[str, Any]) -> dict[str, Any]:
    """Answer that the worker is alive."""
    return {"pong": True}


OPERATIONS: dict[str, Operation] = {
    "ping": ping,
    **docker_ops.OPERATIONS,
    **docker_control.OPERATIONS,
    **git_ops.OPERATIONS,
    **image_ops.OPERATIONS,
    **inspect_ops.OPERATIONS,
    **projects_ops.OPERATIONS,
    **traefik_ops.OPERATIONS,
    **sites_ops.OPERATIONS,
    **certs_ops.OPERATIONS,
    **compose_ops.OPERATIONS,
    **uptime_ops.OPERATIONS,
    **backup_ops.OPERATIONS,
    **stats_ops.OPERATIONS,
    **terminal_ops.OPERATIONS,
    **traffic_ops.OPERATIONS,
    **accesslog_ops.OPERATIONS,
    **nodeconfig.OPERATIONS,
}

# Every operation runs with the node's settings that the hub sent along (see `nodeconfig`).
OPERATIONS = {name: nodeconfig.scoped(operation) for name, operation in OPERATIONS.items()}
