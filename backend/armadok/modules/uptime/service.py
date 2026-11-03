"""Running one probe and deciding what its result means."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.core.signals import announce
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.uptime import repository as repo
from armadok.modules.workers.routes import call_operation

log = logging.getLogger("armadok.uptime")
DOWN_AFTER = 2  # failed checks in a row before a site is called down (one blip is not an outage)
UP_AFTER = 2  # good checks in a row before a down site is called back (no down/up flapping)
TIMEOUT_SECONDS = 5
# Worker answers that mean "the target did not answer": they are results of the probe. Any other
# error (worker offline, node suspended, busy) says nothing about the target and is not recorded.
TARGET_REASONS = {
    "worker.probe_dns": "dns",
    "worker.probe_refused": "refused",
    "worker.probe_timeout": "timeout",
    "worker.probe_tls": "tls",
    "worker.probe_failed": "failed",
    "worker.probe_address_not_allowed": "address_not_allowed",
}
_in_flight: set[str] = set()


class ProbeAnswer(BaseModel):
    """What a worker may say about a probe. It is untrusted: anything else is a failed probe."""

    model_config = ConfigDict(extra="ignore")
    status: int | None = Field(default=None, ge=100, le=599)
    latency_ms: int | None = Field(default=None, ge=0, le=3_600_000)


def operation_for(probe: Any) -> tuple[str, dict[str, Any]]:
    """Return the worker operation and arguments for a stored probe."""
    args: dict[str, Any] = {
        "host": probe["host"],
        "port": probe["port"],
        "timeout": TIMEOUT_SECONDS,
    }
    if probe["kind"] == "tcp":
        return "uptime.tcp", args
    args.update(scheme=probe["scheme"], path=probe["path"], verify_tls=bool(probe["verify_tls"]))
    return "uptime.http", args


def judge(probe: Any, answer: ProbeAnswer) -> tuple[bool, str | None]:
    """Decide if an answer is a success: the expected status, or any status below 400."""
    if probe["kind"] == "tcp":
        return True, None
    if answer.status is None:
        return False, "failed"
    wanted = probe["expect_status"]
    if (answer.status == wanted) if wanted else (answer.status < 400):
        return True, None
    return False, f"http_{answer.status}"


def busy(probe_id: str) -> bool:
    """Return True while a check of this probe is running (a second one would race it)."""
    return probe_id in _in_flight


async def check(app: Any, probe: Any) -> bool | None:
    """Probe once, store the result and raise a notification when the state changes.

    Returns the verdict, or None when the target could not be asked at all (or a check of the
    same probe is already running).
    """
    if probe["id"] in _in_flight:
        return None
    _in_flight.add(probe["id"])
    try:
        return await _check(app, probe)
    finally:
        _in_flight.discard(probe["id"])


async def _check(app: Any, probe: Any) -> bool | None:
    registry = app.state.registry
    op, args = operation_for(probe)
    latency: int | None = None
    try:
        raw = await call_operation(app, registry, probe["node_id"], op, args)
        try:
            answer = ProbeAnswer.model_validate(raw)
            latency = answer.latency_ms
            ok, reason = judge(probe, answer)
        except ValidationError:
            ok, reason = False, "failed"
    except ApiError as error:
        reason = TARGET_REASONS.get(error.code)
        if reason is None:
            return None
        ok = False
    conn = app.state.settings.open_db()
    try:
        current = repo.get(conn, probe["id"])
        if current is None or not current["enabled"]:  # deleted or paused while the check ran
            return ok
        repo.record_result(conn, probe["id"], ok, latency, reason)
        state = current["state"]
        if ok:
            failures, successes = 0, current["successes"] + 1
            new_state = "up" if state != "down" or successes >= UP_AFTER else "down"
        else:
            failures, successes = current["failures"] + 1, 0
            new_state = "down" if failures >= DOWN_AFTER else state
        repo.set_state(conn, probe["id"], new_state, failures, successes, None if ok else reason)
        node = nodes_repo.get_node(conn, probe["node_id"])
    finally:
        conn.close()
    await announce(app, "uptime.changed", probe["node_id"])
    if node is not None and new_state != state and (new_state == "down" or state == "down"):
        await emit(
            app,
            "notify",
            event="uptime.down" if new_state == "down" else "uptime.up",
            node_id=probe["node_id"],
            # `reason` is one of our own short codes (never text from the target).
            params={"node": node["name"], "name": probe["name"], "reason": reason or "-"},
        )
    return ok
