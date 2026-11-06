"""Types shared by every worker operation module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Operation = Callable[[dict[str, Any]], dict[str, Any]]


class OperationError(Exception):
    """A failure the hub may be told about, as a stable `worker.<name>` code.

    Any other exception is reported as a generic failure: its details stay on the node.
    """

    def __init__(self, code: str) -> None:
        """Keep the code (must look like `worker.something`)."""
        super().__init__(code)
        self.code = code
