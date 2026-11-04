"""Running a scan and telling the administrators about what is new."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.events import emit
from armadok.modules.vulns import osv, repository

log = logging.getLogger("armadok.vulns")
SCAN_EVERY = timedelta(hours=24)
CHECK_EVERY_SECONDS = 3600
STARTUP_DELAY_SECONDS = 180
# Unrated findings are announced too: a missing label must not hide a serious flaw.
ALERT_SEVERITIES = ("critical", "high", "unknown")
RETRY_AFTER_FAILURE = timedelta(hours=1)
_running = asyncio.Lock()


class ScanFailed(Exception):
    """The scan could not complete; `code` says why."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        super().__init__(code)
        self.code = code


async def run_scan(app: Any) -> int:
    """Scan now; return how many serious findings were announced. Raises `ScanFailed`."""
    if _running.locked():
        raise ScanFailed("vulns.busy")
    async with _running:
        python = await asyncio.to_thread(osv.installed_components)
        web = await asyncio.to_thread(osv.npm_components)
        # Both lists share one table: the web packages carry a prefix so that a name used by
        # both ecosystems cannot be mistaken for the other.
        components = [*python, *((osv.NPM_PREFIX + n, v) for n, v in web)]
        try:
            findings, truncated = await osv.scan(python)
            if web:
                web_findings, web_truncated = await osv.scan(web, ecosystem="npm")
                findings += [
                    {**f, "component": osv.NPM_PREFIX + f["component"]} for f in web_findings
                ]
                truncated |= {(osv.NPM_PREFIX + n, v) for n, v in web_truncated}
        except osv.OsvError as error:
            code = str(error)
        except Exception:  # noqa: BLE001  (an odd answer must never leave the scan unrecorded)
            log.exception("vulnerability scan crashed")
            code = "vulns.bad_answer"
        else:
            code = ""
        conn = app.state.settings.open_db()
        try:
            if code:
                repository.set_scan(conn, False, code, len(components))
                raise ScanFailed(code)
            repository.store(conn, findings, set(components), truncated)
            serious = repository.unnotified_serious(conn, ALERT_SEVERITIES)
            ids = [row["id"] for row in serious]
        finally:
            conn.close()
    if serious:
        order = {"critical": 0, "high": 1, "unknown": 2}
        worst = min(serious, key=lambda r: order[r["severity"]])
        await emit(
            app,
            "notify",
            event="vulns.found",
            node_id=None,
            params={
                "count": len(serious),
                "component": worst["component"],
                "severity": worst["severity"],
            },
        )
        conn = app.state.settings.open_db()
        try:
            repository.mark_notified(conn, ids)
        finally:
            conn.close()
    return len(serious)


def due(app: Any) -> bool:
    """Return True if the check is on and the last scan is old enough (or there was none)."""
    if not app.state.settings.vuln_check:
        return False
    conn = app.state.settings.open_db()
    try:
        last = repository.last_scan(conn)
    finally:
        conn.close()
    if last is None or not last["finished_at"]:
        return True
    wait = SCAN_EVERY if last["ok"] else RETRY_AFTER_FAILURE  # a failure is retried sooner
    return datetime.now(UTC) - datetime.fromisoformat(last["finished_at"]) >= wait


async def watch_loop(app: Any) -> None:
    """Scan once a day when the check is switched on."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            if due(app):
                await run_scan(app)
        except ScanFailed as error:
            log.warning("vulnerability scan failed: %s", error.code)
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("vulnerability scan round failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
