"""Watching certificates in the background and warning before they expire."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.modules.certs import schemas, service
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.workers.routes import call_operation

log = logging.getLogger("armadok.certs")
CHECK_EVERY_SECONDS = 6 * 3600
STARTUP_DELAY_SECONDS = 60  # let the workers connect before the first check
NODE_TYPES = ("network", "full")
MAX_NAMES_SHOWN = 3


def _superseded(cert: schemas.Certificate, certs: list[schemas.Certificate]) -> bool:
    """Return True if a healthy certificate already covers every name of `cert`.

    Traefik keeps old certificates in acme.json after renewing; they must not raise alarms.
    """
    healthy = [c for c in certs if service.days_left(c) >= service.WARN_DAYS and c is not cert]
    return all(any(service.covers(h, n) for c in healthy for h in c.names) for n in cert.names)


def _summary(group: list[schemas.Certificate]) -> tuple[str, int]:
    """Describe a group: its most urgent names first (and how many more), and the fewest days."""
    ordered = sorted(group, key=lambda c: c.not_after)
    names = list(dict.fromkeys(n for c in ordered for n in c.names))
    shown = ", ".join(names[:MAX_NAMES_SHOWN])
    more = f" (+{len(names) - MAX_NAMES_SHOWN})" if len(names) > MAX_NAMES_SHOWN else ""
    return shown + more, service.days_left(ordered[0])


async def check_certificates(app: Any) -> int:
    """Warn about certificates that expire soon or already expired; return how many warnings."""
    conn = app.state.settings.open_db()
    try:
        nodes = [n for n in nodes_repo.list_nodes(conn) if n["type"] in NODE_TYPES]
    finally:
        conn.close()
    warned = 0
    for node in nodes:
        try:
            result = await call_operation(app, app.state.registry, node["id"], "certs.list", {})
            answer = schemas.CertificateList.model_validate(result)
        except ApiError as error:
            # Not being able to check is worth knowing about, but only the code is logged.
            log.warning("certificate check skipped for node %s: %s", node["id"], error.code)
            continue
        except ValueError:
            log.warning("certificate check skipped for node %s: invalid answer", node["id"])
            continue
        if answer.truncated:
            log.warning("node %s has more certificates than are watched", node["id"])
        certs = answer.certificates
        live = [c for c in certs if not _superseded(c, certs)]
        expired = [c for c in live if service.days_left(c) < 0]
        expiring = [c for c in live if 0 <= service.days_left(c) < service.WARN_DAYS]
        for event, group in (("cert.expired", expired), ("cert.expiring", expiring)):
            if not group:
                continue
            names, days = _summary(group)
            await emit(
                app,
                "notify",
                event=event,
                node_id=node["id"],
                params={"node": node["name"], "names": names, "days": days},
            )
            warned += 1
    return warned


async def watch_loop(app: Any) -> None:
    """Check certificates shortly after start, then every few hours, until the application stops."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await check_certificates(app)
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("certificate check failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
