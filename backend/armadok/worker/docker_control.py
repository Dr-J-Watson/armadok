"""Docker operations that change something: start, stop, restart or remove a container.

A closed set of actions on one container, named by its id. Nothing else is ever sent to the
daemon: no create, no exec, no volume or image removal. Removing a container never removes
its volumes, and never forces (a running container must be stopped first).
"""

from __future__ import annotations

import re
import socket
from typing import Any

from armadok.worker.docker_ops import container_id, docker_get, docker_raw
from armadok.worker.operation import Operation, OperationError

STOP_GRACE_SECONDS = 10
# Stopping waits for the grace period, so the request may legitimately take longer.
# Kept under the hub's 30 s call limit, so the hub never gives up on an action that went through.
ACTION_TIMEOUT_SECONDS = STOP_GRACE_SECONDS + 15

# action -> (HTTP method, path suffix, accepted statuses). 304 means "already in that state".
_ACTIONS: dict[str, tuple[str, str, tuple[int, ...]]] = {
    "start": ("POST", "/start", (204, 304)),
    "stop": ("POST", f"/stop?t={STOP_GRACE_SECONDS}", (204, 304)),
    "restart": ("POST", f"/restart?t={STOP_GRACE_SECONDS}", (204,)),
    "remove": ("DELETE", "?v=0&force=0&link=0", (204,)),
}
FULL_ID = re.compile(r"[0-9a-f]{64}", re.ASCII)
PROTECT_LABEL = "armadok.protect"
_CONTAINER_IN_MOUNTINFO = re.compile(r"/containers/([0-9a-f]{64})/")

# Actions that would cut the worker's own container (and with it the way to undo the action).
_SELF_HARM = frozenset({"stop", "restart", "remove"})


def own_container_id() -> str | None:
    """Return the full id of the container this process runs in, or None outside Docker.

    The mount table names it whatever hostname or network mode was chosen; the hostname (the
    short id by default) is the fallback.
    """
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as table:
            found = _CONTAINER_IN_MOUNTINFO.search(table.read())
        if found:
            return found.group(1)
    except OSError:
        pass
    name = socket.gethostname()
    if re.fullmatch(r"[0-9a-f]{12,64}", name):
        try:
            own = docker_get(f"/containers/{name}/json")
        except OperationError:
            return None
        return own.get("Id") if isinstance(own, dict) else None
    return None


def _is_protected(full_id: str, target: dict[str, Any]) -> bool:
    """Tell whether the container is this worker's own or carries the `armadok.protect` label.

    The label lets an operator also shield the hub and the proxy in front of it.
    """
    labels = (target.get("Config") or {}).get("Labels") or {}
    if isinstance(labels, dict) and str(labels.get(PROTECT_LABEL, "")).lower() == "true":
        return True
    return full_id == own_container_id()


def container_action(args: dict[str, Any]) -> dict[str, Any]:
    """Run one action on one container. Inputs: `id`, `action`. Output: `{"done": True}`.

    Errors: `worker.invalid_path` (bad id or action), `worker.forbidden` (the worker's own
    container), `worker.not_found`, `worker.conflict` (e.g. removing a running container),
    `worker.docker_error`.
    """
    ident = container_id(args.get("id"))
    action = args.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise OperationError("worker.invalid_path")
    # Resolve to the full id first: a name or a short prefix cannot slip past the checks below.
    target = docker_get(f"/containers/{ident}/json")
    full_id = target.get("Id") if isinstance(target, dict) else None
    if not isinstance(full_id, str) or not FULL_ID.fullmatch(full_id):
        raise OperationError("worker.docker_error")
    if action in _SELF_HARM and _is_protected(full_id, target):
        raise OperationError("worker.forbidden")
    ident = full_id
    method, suffix, accepted = _ACTIONS[action]
    status, _ = docker_raw(method, f"/containers/{ident}{suffix}", ACTION_TIMEOUT_SECONDS)
    if status == 404:
        raise OperationError("worker.not_found")
    if status == 409:
        raise OperationError("worker.conflict")
    if status not in accepted:
        raise OperationError("worker.docker_error")
    return {"done": True}


OPERATIONS: dict[str, Operation] = {"docker.container.action": container_action}
