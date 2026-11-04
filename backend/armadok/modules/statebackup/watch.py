"""The background loop that makes the automatic backup."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.modules.statebackup import archive

log = logging.getLogger("armadok.statebackup")
CHECK_EVERY_SECONDS = 15 * 60
STARTUP_DELAY_SECONDS = 120


def due(settings: Any) -> bool:
    """Return True when the newest backup is older than the configured interval (or none)."""
    if settings.state_backup_hours <= 0 or not settings.encryption_key:
        return False
    newest = archive.listing(settings)
    if not newest:
        return True
    stamp = datetime.strptime(newest[0]["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return datetime.now(UTC) - stamp >= timedelta(hours=settings.state_backup_hours)


async def run_once(app: Any) -> str | None:
    """Make the automatic backup if one is due; return its name. A failure is notified."""
    settings = app.state.settings
    if not due(settings):
        return None
    try:
        return await asyncio.to_thread(archive.create, settings)
    except Exception as error:  # noqa: BLE001  (whatever went wrong, the admin must hear of it)
        code = error.code if isinstance(error, ApiError) else "statebackup.write_failed"
        if code == "statebackup.too_soon":
            return None  # a manual backup was being made: nothing is wrong
        log.warning("automatic backup failed: %s", code)
        await emit(app, "notify", event="statebackup.failed", node_id=None, params={"code": code})
        return None


async def watch_loop(app: Any) -> None:
    """Check every quarter of an hour whether the automatic backup is due."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await run_once(app)
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("automatic backup round failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
