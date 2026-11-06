"""Share one slow computation between everyone who asks for it at the same time.

Used for read-only figures that are costly to measure (a node's resources, the statistics of
its containers): many viewers polling the same node cost one measurement, not one each. The
answer is also kept for a few seconds. The access check always runs *before* this is asked, so
sharing an answer never shares it with someone who may not see it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

MAX_KEYS = 2000


class Shared:
    """A small cache of results with single-flight: one run at a time per key."""

    def __init__(self, ttl_seconds: float) -> None:
        """Keep each answer `ttl_seconds` (failures are never kept)."""
        self._ttl = ttl_seconds
        self._done: dict[str, tuple[float, Any]] = {}
        self._running: dict[str, asyncio.Task] = {}

    async def get(self, key: str, make: Callable[[], Awaitable[Any]]) -> Any:
        """Return the kept answer, join the run in progress, or start one.

        The run is its own task, so a caller that gives up (a closed browser tab) does not
        cancel it for the others who are waiting.
        """
        kept = self._done.get(key)
        if kept and time.monotonic() - kept[0] < self._ttl:
            return kept[1]
        task = self._running.get(key)
        if task is None:
            task = asyncio.ensure_future(make())
            self._running[key] = task
            task.add_done_callback(lambda finished, k=key: self._finished(k, finished))
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task) -> None:
        self._running.pop(key, None)
        if task.cancelled() or task.exception() is not None:
            return  # a failure is not kept (reading it also marks it as handled)
        if len(self._done) > MAX_KEYS:
            self._done.clear()
        self._done[key] = (time.monotonic(), task.result())
