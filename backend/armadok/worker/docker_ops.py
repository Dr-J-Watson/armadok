"""Read-only Docker operations of the worker.

Talks to the Docker daemon over its Unix socket with plain HTTP, using GET requests only:
nothing here can start, stop or change anything. Answers are reduced to a fixed set of fields
(no environment variables, no command line), so secrets passed to containers stay on the machine.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any

from armadok.worker.operation import Operation, OperationError
from armadok.worker_protocol.docker_labels import filter_labels

DEFAULT_SOCKET = "/var/run/docker.sock"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CONTAINERS = 1000
# Every string is cut and every list capped, so one hostile or very large host cannot make
# an answer too big for the channel (1 MiB per frame).
MAX_TEXT = 300
MAX_PATH = 1000
MAX_NAMES = 5
MAX_PORTS = 50
MAX_NETWORKS = 20
MAX_MOUNTS = 50
# Budget for the whole answer, well under the frame limit; extra containers are left out.
ANSWER_BUDGET_BYTES = 900 * 1024


class _UnixHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection over a Unix socket (how the Docker daemon listens)."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker_get(path: str) -> Any:
    """GET a Docker API path and return the decoded JSON.

    The socket path comes from `ARMADOK_DOCKER_SOCKET`. Only GET exists on purpose.
    """
    socket_path = os.environ.get("ARMADOK_DOCKER_SOCKET", DEFAULT_SOCKET)
    conn = _UnixHTTPConnection(socket_path, TIMEOUT_SECONDS)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException):
        raise OperationError("worker.docker_unavailable") from None
    finally:
        conn.close()
    if response.status != 200 or len(body) > MAX_RESPONSE_BYTES:
        raise OperationError("worker.docker_error")
    try:
        return json.loads(body)
    except ValueError:
        raise OperationError("worker.docker_error") from None


def docker_info(args: dict[str, Any]) -> dict[str, Any]:
    """Return the Docker version of this machine."""
    data = docker_get("/version")
    return {"version": str(data.get("Version", "")), "api_version": str(data.get("ApiVersion", ""))}


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    """Return `value` as a string cut to `limit` characters."""
    return str(value if value is not None else "")[:limit]


def _number(value: Any) -> int:
    """Return `value` if it is a non-negative integer, else 0."""
    return value if type(value) is int and value >= 0 else 0


def _container(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce one container description to the fields the hub may see."""
    networks = (raw.get("NetworkSettings") or {}).get("Networks") or {}
    return {
        "id": _text(raw.get("Id"), 64),
        "names": [_text(n).lstrip("/") for n in (raw.get("Names") or [])[:MAX_NAMES]],
        "image": _text(raw.get("Image")),
        "state": _text(raw.get("State"), 30),
        "status": _text(raw.get("Status")),
        "created": _number(raw.get("Created")),
        "labels": filter_labels(raw.get("Labels") or {}),
        "ports": [
            {
                "private": p.get("PrivatePort"),
                "public": p.get("PublicPort"),
                "type": _text(p.get("Type"), 10),
                "ip": _text(p.get("IP"), 64),
            }
            for p in (raw.get("Ports") or [])[:MAX_PORTS]
        ],
        "networks": [_text(n) for n in sorted(networks)[:MAX_NETWORKS]],
        "mounts": [
            {
                "type": _text(m.get("Type"), 20),
                "name": _text(m.get("Name")),
                "source": _text(m.get("Source"), MAX_PATH) if m.get("Type") == "bind" else "",
                "destination": _text(m.get("Destination"), MAX_PATH),
                "rw": bool(m.get("RW", False)),
            }
            for m in (raw.get("Mounts") or [])[:MAX_MOUNTS]
        ],
    }


def containers_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return containers (running or not) with a reduced, safe description.

    Stops adding containers once the answer would exceed the byte budget and says so with
    `truncated`, rather than letting an oversized answer break the connection.
    """
    data = docker_get("/containers/json?all=1")
    if not isinstance(data, list):
        raise OperationError("worker.docker_error")
    containers: list[dict[str, Any]] = []
    used = 0
    for raw in data[:MAX_CONTAINERS]:
        if not isinstance(raw, dict):
            continue
        container = _container(raw)
        size = len(json.dumps(container))
        if used + size > ANSWER_BUDGET_BYTES:
            return {"containers": containers, "truncated": True}
        containers.append(container)
        used += size
    return {"containers": containers, "truncated": False}


OPERATIONS: dict[str, Operation] = {
    "docker.info": docker_info,
    "docker.containers.list": containers_list,
}
