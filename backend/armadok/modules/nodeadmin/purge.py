"""Background loop that erases nodes deleted longer ago than the restore window (30 days)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from armadok.core import audit
from armadok.modules.nodes import repository as nodes_repo

log = logging.getLogger("armadok.nodeadmin")
CHECK_EVERY_SECONDS = 6 * 3600
STARTUP_DELAY_SECONDS = 300


def _purge_all(app: Any) -> int:
    """Erase every node whose restore window is over (runs in a thread); return how many."""
    conn = app.state.settings.open_db()
    try:
        done = 0
        for node_id in nodes_repo.purgeable(conn):

            def record(node_id: str = node_id) -> None:
                audit.record(
                    conn,
                    app.state.registry.audit_events(),
                    "system",
                    "node.purge",
                    object_type="node",
                    object_id=node_id,
                    details={"reason": "restore window over"},
                    commit=False,
                )

            try:
                if nodes_repo.purge(conn, node_id, app.state.registry.node_tables(), record):
                    done += 1
            except Exception:  # noqa: BLE001  (one stubborn node must not stop the others)
                log.exception("could not erase an expired node")
        return done
    finally:
        conn.close()


async def purge_expired(app: Any) -> int:
    """Erase the nodes whose restore window is over, off the event loop; return how many."""
    return await asyncio.to_thread(_purge_all, app)


async def purge_loop(app: Any) -> None:
    """Look every few hours for nodes whose 30 days are over."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await purge_expired(app)
        except Exception:  # noqa: BLE001  (keep the loop alive)
            log.exception("purge round failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
