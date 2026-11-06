"""Availability probes, run from the node that hosts the site.

A probe only answers "did it respond, how fast, with which status": no body, no header and no
address ever goes back to the hub. Because the request starts from the worker, a probe can
reach what the node reaches (a container on a private network, a site behind Traefik), and
that is exactly why creating one needs a sensitive right on the hub.

What is still refused here, whatever the hub says:

- other schemes than `http` and `https`, credentials in the address, ports outside 1-65535;
- link-local, multicast and unspecified addresses and the known cloud metadata addresses
  (169.254.169.254 and its IPv6 and translated forms); loopback and private ranges stay
  allowed, they are what is monitored;
- redirects are not followed (the answer of the first hop is the answer);
- one probe has one overall deadline (name resolution included), only the status line of the
  answer is read, and at most a few probes run at once on a worker;
- the name is resolved once and the connection goes to that very address, so a name that
  changes its answer between the check and the connection cannot slip through.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import threading
import time
from typing import Any

from armadok.worker.operation import Operation, OperationError

HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:]{2,45}")
PATH = re.compile(r"/[\x21-\x7e]{0,500}")  # printable ASCII, no space or control character
STATUS_LINE = re.compile(rb"HTTP/1\.[01] ([1-5][0-9]{2})[ \r]")
MAX_TIMEOUT_SECONDS = 10
MAX_AT_ONCE = 8
MAX_STATUS_BYTES = 4096
# Known metadata addresses that are not link-local, and the translated forms of 169.254.0.0/16.
DENIED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "fd00:ec2::/32",  # AWS IPv6 metadata
        "2002:a9fe::/32",  # 6to4 form of 169.254.0.0/16
        "64:ff9b::/96",  # NAT64: reaches any IPv4 address, link-local ones included
        "240.0.0.0/4",
    )
)
_slots = threading.BoundedSemaphore(MAX_AT_ONCE)


def refused(address: str) -> bool:
    """Return True for addresses a probe must never touch."""
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or any(ip in net for net in DENIED if net.version == ip.version)
    )


def _resolve(host: str, port: int, deadline: float) -> list[tuple[int, str]]:
    """Return the `(family, address)` pairs of `host`, all acceptable, within the deadline.

    `getaddrinfo` cannot be interrupted, so it runs in a helper thread we stop waiting for.
    """
    box: list[Any] = []

    def work() -> None:
        try:
            box.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            box.append(None)

    helper = threading.Thread(target=work, daemon=True)
    helper.start()
    helper.join(max(0.0, deadline - time.monotonic()))
    if not box:
        raise OperationError("worker.probe_timeout")
    if not box[0]:
        raise OperationError("worker.probe_dns")
    found = [(family, sockaddr[0]) for family, _, _, _, sockaddr in box[0]]
    if any(refused(address) for _, address in found):
        raise OperationError("worker.probe_address_not_allowed")
    return found


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    """Connect to the first address that answers; every address was checked beforehand."""
    failure = "worker.probe_refused"
    for family, address in _resolve(host, port, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise OperationError("worker.probe_timeout")
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(left)
        try:
            sock.connect((address, port))
            return sock
        except TimeoutError:
            failure = "worker.probe_timeout"
        except OSError:
            pass
        sock.close()
    raise OperationError(failure)


def _args(args: dict[str, Any]) -> tuple[str, int, float]:
    host, port, timeout = args.get("host"), args.get("port"), args.get("timeout", 5)
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise OperationError("worker.invalid_path")
    if type(port) is not int or not 1 <= port <= 65535:
        raise OperationError("worker.invalid_path")
    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise OperationError("worker.invalid_path")
    return host, port, float(timeout)


def _limited(run: Operation) -> Operation:
    """Refuse (instead of queueing) when too many probes already run on this worker."""

    def guarded(args: dict[str, Any]) -> dict[str, Any]:
        if not _slots.acquire(blocking=False):
            raise OperationError("worker.busy")
        try:
            return run(args)
        finally:
            _slots.release()

    return guarded


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise OperationError("worker.probe_timeout")
    return left


def _read_status(sock: socket.socket, deadline: float) -> int:
    """Read just enough of the answer to get its status code, within the deadline."""
    data = b""
    while len(data) < MAX_STATUS_BYTES:
        sock.settimeout(_remaining(deadline))
        try:
            chunk = sock.recv(1024)
        except TimeoutError:
            raise OperationError("worker.probe_timeout") from None
        except (OSError, ssl.SSLError):
            raise OperationError("worker.probe_failed") from None
        if not chunk:
            break
        data += chunk
        found = STATUS_LINE.match(data)
        if found:
            return int(found.group(1))
        if b"\n" in data:
            break
    raise OperationError("worker.probe_failed")


@_limited
def probe_tcp(args: dict[str, Any]) -> dict[str, Any]:
    """Open a TCP connection and close it: "is something listening?"."""
    host, port, timeout = _args(args)
    started = time.monotonic()
    _connect(host, port, started + timeout).close()
    return {"status": None, "latency_ms": int((time.monotonic() - started) * 1000)}


@_limited
def probe_http(args: dict[str, Any]) -> dict[str, Any]:
    """Send `GET <path>` and return the status code of the answer, without reading the body."""
    host, port, timeout = _args(args)
    scheme, path = args.get("scheme"), args.get("path", "/")
    if scheme not in ("http", "https") or not isinstance(path, str) or not PATH.fullmatch(path):
        raise OperationError("worker.invalid_path")
    verify = args.get("verify_tls", True)
    if not isinstance(verify, bool):
        raise OperationError("worker.invalid_path")
    started = time.monotonic()
    deadline = started + timeout
    sock = _connect(host, port, deadline)
    try:
        if scheme == "https":
            context = ssl.create_default_context()
            if not verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            sock.settimeout(_remaining(deadline))
            try:
                sock = context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLError:
                raise OperationError("worker.probe_tls") from None
            except TimeoutError:
                raise OperationError("worker.probe_timeout") from None
            except OSError:
                raise OperationError("worker.probe_refused") from None
        request = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUser-Agent: armadok-probe\r\n"
        sock.settimeout(_remaining(deadline))
        try:
            sock.sendall((request + "Connection: close\r\n\r\n").encode("ascii"))
        except TimeoutError:
            raise OperationError("worker.probe_timeout") from None
        except OSError:
            raise OperationError("worker.probe_failed") from None
        status = _read_status(sock, deadline)
    finally:
        sock.close()
    return {"status": status, "latency_ms": int((time.monotonic() - started) * 1000)}


OPERATIONS: dict[str, Operation] = {"uptime.tcp": probe_tcp, "uptime.http": probe_http}
