"""Reading Traefik's Prometheus metrics, on the node that runs it.

The metrics endpoint is private to the node (an entry point that is not published), so the
worker is the one that can read it: it makes a single `GET` to the address the operator gave in
`ARMADOK_TRAEFIK_METRICS_URL`, never to one the hub names. What comes back is reduced to counters
per service (requests by status class, duration buckets, bytes) and a few health facts; the hub
turns the ever-growing counters into per-interval figures. Nothing about visitors is in these
metrics: they carry no IP address.
"""

from __future__ import annotations

import http.client
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from armadok.worker.operation import Operation, OperationError

TIMEOUT_SECONDS = 5
TOTAL_SECONDS = 15
MAX_BYTES = 4 * 1024 * 1024
MAX_SERVICES = 200
MAX_BUCKETS = 24
MAX_SERVERS = 400
_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_CLASSES = {"1": "other", "2": "2xx", "3": "3xx", "4": "4xx", "5": "5xx"}
_FAMILIES = {
    "traefik_service_requests_total": "requests",
    "traefik_service_request_duration_seconds_bucket": "bucket",
    "traefik_service_request_duration_seconds_sum": "sum",
    "traefik_service_request_duration_seconds_count": "count",
    "traefik_service_requests_bytes_total": "bytes_in",
    "traefik_service_responses_bytes_total": "bytes_out",
}


def _url() -> tuple[str, str, int, str]:
    configured = os.environ.get("ARMADOK_TRAEFIK_METRICS_URL")
    if not configured:
        raise OperationError("worker.metrics_not_configured")
    try:
        parts = urlsplit(configured)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise OperationError("worker.metrics_not_configured") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise OperationError("worker.metrics_not_configured")
    return parts.scheme, parts.hostname, port, (parts.path or "/metrics")


def _fetch() -> str:
    scheme, host, port, path = _url()
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=TIMEOUT_SECONDS)
    try:
        conn.request("GET", path, headers={"Accept": "text/plain"})
        response = conn.getresponse()
        if response.status != 200:
            raise OperationError("worker.metrics_unreachable")
        # Read in pieces under one deadline: a server dripping bytes is cut off, not waited for.
        deadline = time.monotonic() + TOTAL_SECONDS
        chunks, size = [], 0
        while size <= MAX_BYTES:
            if time.monotonic() > deadline:
                raise OperationError("worker.metrics_unreachable")
            piece = response.read(65536)
            if not piece:
                break
            chunks.append(piece)
            size += len(piece)
        data = b"".join(chunks)
    except (OSError, http.client.HTTPException):
        raise OperationError("worker.metrics_unreachable") from None
    finally:
        conn.close()
    if len(data) > MAX_BYTES:
        raise OperationError("worker.too_large")
    return data.decode("utf-8", "replace")


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def parse(text: str) -> dict[str, Any]:
    """Reduce Prometheus text to per-service counters and a few health facts."""
    services: dict[str, dict[str, Any]] = {}
    servers: dict[str, dict[str, int]] = {}
    # Traefik gives the TIME of the last good and the last failed reload (not a 0/1 flag).
    last_success: float | None = None
    last_failure: float | None = None
    reload_failures = 0.0

    def entry(name: str) -> dict[str, Any] | None:
        if name not in services and len(services) >= MAX_SERVICES:
            return None
        return services.setdefault(
            name,
            {
                "requests": {},
                "buckets": {},
                "sum": 0.0,
                "count": 0.0,
                "bytes_in": 0.0,
                "bytes_out": 0.0,
            },
        )

    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        found = _LINE.match(line)
        if not found:
            continue
        name, raw_labels, raw_value = found.groups()
        value = _number(raw_value)
        if value is None:
            continue
        labels = {
            k: v.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
            for k, v in _LABEL.findall(raw_labels or "")
        }
        if name == "traefik_config_last_reload_success":
            last_success = value
        elif name == "traefik_config_last_reload_failure":
            last_failure = value
        elif name == "traefik_config_reloads_failure_total":
            reload_failures += value
        elif name == "traefik_service_server_up" and "service" in labels:
            bucket = servers.setdefault(labels["service"][:200], {"up": 0, "down": 0})
            bucket["up" if value == 1 else "down"] += 1
            if len(servers) > MAX_SERVERS:
                servers.pop(next(iter(servers)))
        elif name in _FAMILIES and "service" in labels:
            service = entry(labels["service"][:200])
            if service is None:
                continue
            kind = _FAMILIES[name]
            if kind == "bucket":
                if "le" in labels:
                    le = labels["le"][:12]
                    if le in service["buckets"] or len(service["buckets"]) < MAX_BUCKETS:
                        service["buckets"][le] = service["buckets"].get(le, 0.0) + value
            elif kind == "requests":
                klass = _CLASSES.get(labels.get("code", "0")[:1], "other")
                service["requests"][klass] = service["requests"].get(klass, 0.0) + value
            else:
                service[kind] += value
    reload_ok: bool | None = None
    if last_success is not None or last_failure is not None:
        # The last reload counts as good when no failure came after the last success.
        reload_ok = (last_success or 0) >= (last_failure or 0) and (last_success or 0) > 0
    return {
        "services": services,
        "servers": servers,
        "reload_ok": reload_ok,
        "reload_failures": reload_failures,
    }


def scrape(args: dict[str, Any]) -> dict[str, Any]:
    """Read the metrics now.

    Errors: `worker.metrics_not_configured`, `worker.metrics_unreachable`.
    """
    return {"configured": True, **parse(_fetch())}


def status(args: dict[str, Any]) -> dict[str, Any]:
    """Say whether a metrics address is set up (without calling it)."""
    try:
        _url()
    except OperationError:
        return {"configured": False}
    return {"configured": True}


OPERATIONS: dict[str, Operation] = {
    "traffic.scrape": scrape,
    "traffic.status": status,
}
