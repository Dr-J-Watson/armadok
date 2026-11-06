"""Change signals: "something changed here, look again".

A signal carries no data, only its name (`docker.changed`) and, when it concerns one node,
that node's id. The screen reloads through the API, which applies the rights; so a signal
can never show anyone what they may not see. Modules declare the names they own in
`Module.signals`; an undeclared name is ignored.

Most signals come from one place: `signal_for_request` turns any successful change request
(`POST /nodes/<id>/docker/...`) into the signal of the area it touched, so features do not each
have to remember to announce their changes. Code that changes things on its own (a worker
connecting, a deployment ending) calls `announce`.
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI

from armadok.core.events import emit

log = logging.getLogger("armadok.signals")

CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Starts with a letter, digit, "_" or "-": "." and ".." are path tricks, never ids.
_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}")
_AREA = re.compile(r"[a-z_-]{1,30}")
API_PREFIX = "/api/v1/"
# Requests that use POST only to carry their input (they change nothing): no signal.
READ_ONLY_ENDINGS = ("/preview", "/check", "/test", "/verify", "/search", "/lookup", "/dry-run")


def signal_for_request(method: str, path: str) -> tuple[str, str | None] | None:
    """Return `(signal name, node id or None)` for a request that changes something."""
    if method not in CHANGING_METHODS or not path.startswith(API_PREFIX):
        return None
    if path.rstrip("/").endswith(READ_ONLY_ENDINGS):
        return None
    parts = path[len(API_PREFIX) :].strip("/").split("/")
    node_id: str | None = None
    if parts[0] == "nodes" and len(parts) >= 2 and _ID.fullmatch(parts[1]):
        node_id = parts[1]
        area = parts[2] if len(parts) > 2 else "nodes"
    else:
        area = parts[0]
    if not _AREA.fullmatch(area):
        return None
    return f"{area}.changed", node_id


async def announce(
    app: FastAPI, signal: str, node_id: str | None = None, actor_id: str | None = None
) -> None:
    """Tell the screens that may know.

    Unknown names are dropped; a failure here never fails the change that caused it (it is
    only logged).

    `actor_id` is who made the change; a signal that names no node goes to them alone
    unless the module marked it shared (see `Signal`).
    """
    if signal not in app.state.registry.signals():
        return
    try:
        await emit(app, "signal", signal=signal, node_id=node_id, actor_id=actor_id)
    except Exception:  # noqa: BLE001  (the change is done; a missed refresh is not worth a 500)
        log.exception("could not announce %s", signal)
