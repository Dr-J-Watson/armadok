"""The background loop that follows running backup jobs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from armadok.modules.backups import repository as repo
from armadok.modules.backups import service

log = logging.getLogger("armadok.backups")
TICK_SECONDS = 5
JOB_TIMEOUT_SECONDS = 45
STARTUP_DELAY_SECONDS = 20  # also picks up jobs that were running when the hub stopped


async def _follow(app: Any, job: Any) -> None:
    try:
        await asyncio.wait_for(service.sync_job(app, job), JOB_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001  (a job that cannot be followed now is tried again later)
        log.warning("could not follow backup job %s", job["id"])


async def watch_loop(app: Any) -> None:
    """Follow every running job until the application stops."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            conn = app.state.settings.open_db()
            try:
                jobs = repo.running(conn)
            finally:
                conn.close()
            # Together, each with its own limit: one unreachable node must not hold up the rest.
            await asyncio.gather(*(_follow(app, job) for job in jobs))
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("backup follow-up failed")
        await asyncio.sleep(TICK_SECONDS)
