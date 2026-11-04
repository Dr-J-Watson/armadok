"""Docker operations that change something: start, stop, restart or remove a container.

A closed set of actions on one container, named by its id. Nothing else is ever sent to the
daemon: no create, no exec, no volume or image removal. Removing a container never removes
its volumes, and never forces (a running container must be stopped first).
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any
from urllib.parse import quote

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


# kind -> (daemon path, extra filters). Volumes are never pruned from here: their data is the
# one thing that cannot be recreated. Anything labelled `armadok.protect=true` is left alone
# (where the daemon can filter on it); dangling images only, never every unused image.
_PRUNE: dict[str, tuple[str, dict[str, list[str]]]] = {
    "containers": ("/containers/prune", {"label!": [f"{PROTECT_LABEL}=true"]}),
    "images": ("/images/prune", {"dangling": ["true"]}),
}
# Networks are not pruned by the daemon's own call: it would remove the network of a stopped
# proxy (a stopped container is attached to nothing as far as it can tell). See `_prune_networks`.
_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none"})
MAX_NETWORKS_CHECKED = 500
PRUNE_TIMEOUT_SECONDS = 25  # under the hub's call limit


def prune(args: dict[str, Any]) -> dict[str, Any]:
    """Remove stopped containers, dangling images or unused networks.

    Inputs: `kind`. Output: `{"removed": count, "space": bytes freed}`.
    Errors: `worker.invalid_path` (unknown kind), `worker.docker_error`.
    """
    kind = args.get("kind")
    if kind == "networks":
        return _prune_networks()
    if not isinstance(kind, str) or kind not in _PRUNE:
        raise OperationError("worker.invalid_path")
    path, filters = _PRUNE[kind]
    status, body = docker_raw(
        "POST", f"{path}?filters={quote(json.dumps(filters))}", PRUNE_TIMEOUT_SECONDS
    )
    if status != 200:
        raise OperationError("worker.docker_error")
    try:
        data = json.loads(body)
    except ValueError:
        raise OperationError("worker.docker_error") from None
    if not isinstance(data, dict):
        raise OperationError("worker.docker_error")
    # The daemon names the list differently for each kind; only counts go back to the hub.
    names = (
        data.get("ContainersDeleted") or data.get("ImagesDeleted") or data.get("NetworksDeleted")
    )
    space = data.get("SpaceReclaimed")
    return {
        "removed": len(names) if isinstance(names, list) else 0,
        "space": space if type(space) is int and space >= 0 else 0,
    }


def _json_list(path: str) -> list[Any]:
    """Ask the daemon for a JSON list; anything else is an error."""
    status, body = docker_raw("GET", path, PRUNE_TIMEOUT_SECONDS)
    try:
        data = json.loads(body) if status == 200 else None
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise OperationError("worker.docker_error")
    return data


def _prune_networks() -> dict[str, Any]:
    """Remove networks that nothing uses, keeping what a stopped project will need again.

    Kept: the built-in networks, anything labelled `armadok.protect=true`, and every network
    that any container (running or stopped) is attached to. The last rule is what protects the
    network of a stopped proxy or project, which the daemon's own prune would delete. A network
    that turns out to be in use at the last moment is refused by the daemon and left alone.
    """
    networks = _json_list("/networks")[:MAX_NETWORKS_CHECKED]
    kept: set[str] = set()
    for container in _json_list("/containers/json?all=1"):
        settings = container.get("NetworkSettings") if isinstance(container, dict) else None
        attached = settings.get("Networks") if isinstance(settings, dict) else None
        if not isinstance(attached, dict):
            continue
        for name, info in attached.items():
            kept.add(name)
            if isinstance(info, dict) and isinstance(info.get("NetworkID"), str):
                kept.add(info["NetworkID"])
    removed = 0
    for network in networks:
        if not isinstance(network, dict):
            continue
        name, ident = network.get("Name"), network.get("Id")
        labels = network.get("Labels") if isinstance(network.get("Labels"), dict) else {}
        if (
            not isinstance(name, str)
            or not isinstance(ident, str)
            or not re.fullmatch(r"[0-9a-f]{12,64}", ident)
            or name in _BUILTIN_NETWORKS
            or name in kept
            or ident in kept
            or labels.get(PROTECT_LABEL) == "true"
        ):
            continue
        status, _ = docker_raw("DELETE", f"/networks/{ident}", PRUNE_TIMEOUT_SECONDS)
        if status in (200, 204):
            removed += 1
    return {"removed": removed, "space": 0}


OPERATIONS: dict[str, Operation] = {
    "docker.container.action": container_action,
    "docker.prune": prune,
}
