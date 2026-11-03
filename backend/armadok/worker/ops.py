"""The closed list of operations a worker can run.

Adding an operation = write a function in a module under `armadok/worker/`, add it to that
module's `OPERATIONS`, include the module below, and declare the same name (with its node
types) in the owning hub module's `worker_ops`. Anything not listed is refused.
"""

from __future__ import annotations

from typing import Any

from armadok.worker import docker_ops, projects_ops
from armadok.worker.operation import Operation, OperationError

__all__ = ["OPERATIONS", "Operation", "OperationError"]


def ping(args: dict[str, Any]) -> dict[str, Any]:
    """Answer that the worker is alive."""
    return {"pong": True}


OPERATIONS: dict[str, Operation] = {
    "ping": ping,
    **docker_ops.OPERATIONS,
    **projects_ops.OPERATIONS,
}
