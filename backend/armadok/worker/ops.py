"""The closed list of operations a worker can run.

Adding an operation = write a function here and register it in `OPERATIONS`, and declare
the same name in the owning module's `worker_ops`. Anything not listed is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Operation = Callable[[dict[str, Any]], dict[str, Any]]


def ping(args: dict[str, Any]) -> dict[str, Any]:
    """Answer that the worker is alive."""
    return {"pong": True}


OPERATIONS: dict[str, Operation] = {"ping": ping}
