"""The background loop that runs probes when they are due."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from armadok.modules.uptime import repository as repo
from armadok.modules.uptime import service

log = logging.getLogger("armadok.uptime")
TICK_SECONDS = 10
STARTUP_DELAY_SECONDS = 45  # let the workers connect before the first round
MAX_IN_FLIGHT = 24  # probes running at once on the whole platform
MAX_PER_NODE = 3  # at once on one node: a slow node must not hold up the others
_tasks: set[asyncio.Task] = set()
_per_node: dict[str, asyncio.Semaphore] = {}


async def run_due(app: Any) -> int:
    """Start the probes that are due, without waiting for them; return how many were started.

    Each check is its own task, so a slow node only delays its own probes. A probe still
    running is not started twice.
    """
    room = MAX_IN_FLIGHT - len(_tasks)
    if room <= 0:
        return 0
    conn = app.state.settings.open_db()
    try:
        due = [p for p in repo.due(conn, datetime.now(UTC), room * 2) if not service.busy(p["id"])][
            :room
        ]
        # Marked first: a worker that is slow to answer is not asked again by the next round.
        for probe in due:
            repo.mark_checked(conn, probe["id"])
    finally:
        conn.close()

    async def one(probe: Any) -> None:
        gate = _per_node.setdefault(probe["node_id"], asyncio.Semaphore(MAX_PER_NODE))
        async with gate:
            try:
                await service.check(app, probe)
            except Exception:  # noqa: BLE001  (one failing probe must not stop the others)
                log.exception("probe %s failed to run", probe["id"])

    for probe in due:
        task = asyncio.create_task(one(probe))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return len(due)


async def watch_loop(app: Any) -> None:
    """Run due probes every few seconds until the application stops."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await run_due(app)
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("probe round failed")
        await asyncio.sleep(TICK_SECONDS)
