"""Read-only Docker operations of the worker.

Talks to the Docker daemon over its Unix socket with plain HTTP. Every operation here only
reads (GET): nothing can start, stop or change anything (see `docker_control` for that).
Answers are reduced to a fixed set of fields (no environment variables, no command line), so
secrets passed to containers stay on the machine.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
from typing import Any

from armadok.worker.operation import Operation, OperationError
from armadok.worker_protocol.docker_labels import filter_labels

DEFAULT_SOCKET = "/var/run/docker.sock"
TIMEOUT_SECONDS = 10
MAX_LOG_LINES = 1000
MAX_LOG_BYTES = 128 * 1024
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}", re.ASCII)
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


class ResponseTooLarge(OperationError):
    """The daemon's answer exceeded the size limit (callers may ask for less)."""


def docker_get(path: str, timeout: float = TIMEOUT_SECONDS) -> Any:
    """GET a Docker API path and return the decoded JSON.

    The socket path comes from `ARMADOK_DOCKER_SOCKET`. Only GET exists on purpose.
    """
    status, body = docker_raw("GET", path, timeout)
    if status == 404:
        raise OperationError("worker.not_found")
    if status != 200:
        raise OperationError("worker.docker_error")
    try:
        return json.loads(body)
    except ValueError:
        raise OperationError("worker.docker_error") from None


def docker_raw(
    method: str, path: str, timeout: float = TIMEOUT_SECONDS, body: dict[str, Any] | None = None
) -> tuple[int, bytes]:
    """Send one request to the Docker daemon and return `(status, body)`.

    Shared by the read-only operations here and the control operations (`docker_control`),
    which are the only callers allowed to pass a method other than GET. `body` (a JSON object)
    is sent only by the backup helper, which creates its one fixed kind of container.
    """
    socket_path = os.environ.get("ARMADOK_DOCKER_SOCKET", DEFAULT_SOCKET)
    conn = _UnixHTTPConnection(socket_path, timeout)
    try:
        if body is None:
            conn.request(method, path)
        else:
            conn.request(method, path, json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        answer = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException):
        raise OperationError("worker.docker_unavailable") from None
    finally:
        conn.close()
    if len(answer) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge("worker.docker_error")
    return response.status, answer


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


def _listing(path: str, key: str | None, reduce: Any) -> dict[str, Any]:
    """Return `{"items": [...], "truncated": bool}` for a Docker list endpoint, reduced."""
    data = docker_get(path)
    if key is not None:
        data = data.get(key) if isinstance(data, dict) else None
    if not isinstance(data, list):
        raise OperationError("worker.docker_error")
    items = [reduce(raw) for raw in data[:MAX_CONTAINERS] if isinstance(raw, dict)]
    return {"items": items, "truncated": len(data) > MAX_CONTAINERS}


def images_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return the images (name tags, size, age), nothing about how they were built."""
    return _listing(
        "/images/json",
        None,
        lambda r: {
            "id": _text(r.get("Id"), 80),
            "tags": [_text(t) for t in (r.get("RepoTags") or [])[:MAX_NAMES]],
            "size": _number(r.get("Size")),
            "created": _number(r.get("Created")),
        },
    )


def networks_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return the networks (name, driver, scope); who uses them follows from the containers."""
    return _listing(
        "/networks",
        None,
        lambda r: {
            "id": _text(r.get("Id"), 80),
            "name": _text(r.get("Name")),
            "driver": _text(r.get("Driver"), 30),
            "scope": _text(r.get("Scope"), 20),
            "internal": bool(r.get("Internal", False)),
        },
    )


def volumes_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return the volumes (name, driver); the host path is not shown."""
    return _listing(
        "/volumes",
        "Volumes",
        lambda r: {
            "name": _text(r.get("Name")),
            "driver": _text(r.get("Driver"), 30),
            "created": _text(r.get("CreatedAt"), 40),
        },
    )


def container_logs(args: dict[str, Any]) -> dict[str, Any]:
    """Return the last lines of a container's logs (stdout and stderr, no timestamps)."""
    ident = container_id(args.get("id"))
    tail = args.get("tail", 200)
    if not isinstance(tail, int) or isinstance(tail, bool) or not 1 <= tail <= MAX_LOG_LINES:
        raise OperationError("worker.invalid_path")
    tty = bool((docker_get(f"/containers/{ident}/json").get("Config") or {}).get("Tty"))
    while True:
        try:
            status, body = docker_raw(
                "GET", f"/containers/{ident}/logs?stdout=1&stderr=1&tail={tail}"
            )
            break
        except ResponseTooLarge:
            # Very long lines: ask for fewer of them rather than make the logs unreadable.
            if tail == 1:
                raise
            tail = max(1, tail // 4)
    if status == 404:
        raise OperationError("worker.not_found")
    if status != 200:
        raise OperationError("worker.docker_error")
    text = body if tty else _demultiplex(body)
    truncated = len(text) > MAX_LOG_BYTES
    return {"output": text[-MAX_LOG_BYTES:].decode("utf-8", "replace"), "truncated": truncated}


def _demultiplex(body: bytes) -> bytes:
    """Join the frames of Docker's stream format (8-byte header: type, 3 zeros, 4-byte size)."""
    out: list[bytes] = []
    pos = 0
    while pos + 8 <= len(body):
        size = int.from_bytes(body[pos + 4 : pos + 8], "big")
        out.append(body[pos + 8 : pos + 8 + size])
        pos += 8 + size
    return b"".join(out)


def container_id(value: Any) -> str:
    """Return `value` if it is a full or short container id (hex), else refuse."""
    if not isinstance(value, str) or not CONTAINER_ID.fullmatch(value):
        raise OperationError("worker.invalid_path")
    return value


OPERATIONS: dict[str, Operation] = {
    "docker.info": docker_info,
    "docker.containers.list": containers_list,
    "docker.images.list": images_list,
    "docker.networks.list": networks_list,
    "docker.volumes.list": volumes_list,
    "docker.container.logs": container_logs,
}
