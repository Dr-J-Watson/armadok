"""Endpoints of availability probes, per node.

Reading needs `uptime.read`. Creating, changing and removing a probe needs `uptime.manage`
(sensitive): the probe is a request that starts from the machine, so who may create one is who
may make that machine knock on a door. The addresses link-local services use are refused.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.uptime import repository as repo
from armadok.modules.uptime import service
from armadok.modules.workers.access import authorise
from armadok.worker.uptime_ops import HOST, PATH, refused

router = APIRouter(prefix="/nodes/{node_id}/uptime", tags=["uptime"])

MAX_PROBES_PER_NODE = 50
MAX_PROBES = 500  # on the whole platform: the checker has a fixed capacity
MIN_INTERVAL = 30
MAX_INTERVAL = 3600
RECENT = 60  # results shown with each probe
MIN_CHECK_GAP_SECONDS = 10  # "check now" cannot be hammered
_last_manual: dict[str, float] = {}
ProbeId = Path(pattern=r"^[0-9a-f]{16}$")


class ProbeIn(BaseModel):
    """A probe to create: an HTTP(S) address, or a host and port for a TCP probe."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[^\x00-\x1f\x7f]{1,60}$")
    kind: Literal["http", "tcp"]
    url: str | None = Field(default=None, max_length=600)
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    verify_tls: bool = True
    expect_status: int | None = Field(default=None, ge=100, le=599)
    interval_seconds: int = Field(default=60, ge=MIN_INTERVAL, le=MAX_INTERVAL)


class ProbeChange(BaseModel):
    """What can be changed afterwards (not the target: make a new probe for another one)."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, pattern=r"^[^\x00-\x1f\x7f]{1,60}$")
    enabled: bool | None = None
    verify_tls: bool | None = None
    expect_status: int | None = Field(default=None, ge=100, le=599)
    interval_seconds: int | None = Field(default=None, ge=MIN_INTERVAL, le=MAX_INTERVAL)


def _target(body: ProbeIn) -> dict:
    """Turn the request into stored fields, or refuse it with a code."""
    if body.kind == "tcp":
        if body.host is None or body.port is None:
            raise ApiError("uptime.invalid_target", 422)
        host, port, scheme, path = body.host, body.port, None, "/"
    else:
        if body.url is None:
            raise ApiError("uptime.invalid_target", 422)
        try:
            parts = urlsplit(body.url)
            port = parts.port
            host = parts.hostname
        except ValueError:
            raise ApiError("uptime.invalid_target", 422) from None
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            raise ApiError("uptime.invalid_target", 422)
        # No query string: it often carries a token, and probes are readable by every viewer.
        if parts.fragment or parts.query:
            raise ApiError("uptime.invalid_target", 422)
        scheme = parts.scheme
        port = port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if not PATH.fullmatch(path):
            raise ApiError("uptime.invalid_target", 422)
    if not HOST.fullmatch(host):
        raise ApiError("uptime.invalid_target", 422)
    try:
        if refused(host):
            raise ApiError("uptime.address_not_allowed", 422)
    except ValueError:
        pass  # a name, not an address: the worker checks what it resolves to
    name = body.name.strip()
    if not name:
        raise ApiError("uptime.invalid_target", 422)
    return {
        "name": name,
        "kind": body.kind,
        "scheme": scheme,
        "host": host.lower(),
        "port": port,
        "path": path,
        "verify_tls": int(body.verify_tls),
        "expect_status": body.expect_status,
        "interval_seconds": body.interval_seconds,
    }


def _audit(conn, registry, who, event, node_id, probe_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="probe",
        object_id=probe_id,
        details={"node_id": node_id, **details},
    )


def _probe_of(conn: sqlite3.Connection, node_id: str, probe_id: str) -> sqlite3.Row:
    """Return the probe if it belongs to this node, else NotFound."""
    row = repo.get(conn, probe_id)
    if row is None or row["node_id"] != node_id:
        raise NotFound()
    return row


@router.get("", summary="Availability probes of a node")
def list_probes(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the probes with their state, availability and latest results."""
    authorise(conn, registry, who, node_id, "uptime.read")
    probes = []
    for row in repo.list_for_node(conn, node_id):
        probes.append(
            {
                **repo.view(row),
                "availability": repo.availability(conn, row["id"]),
                "recent": repo.history(conn, row["id"], RECENT),
            }
        )
    return {
        "probes": probes,
        "min_interval": MIN_INTERVAL,
        "max_interval": MAX_INTERVAL,
        # Lets the screen show the controls only to those who can use them; the server still
        # decides on each request.
        "can_manage": who.has("uptime.manage", registry.rights(), f"node:{node_id}"),
    }


@router.post("", status_code=201, summary="Create a probe")
def create_probe(
    node_id: str,
    body: ProbeIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a probe; the first check runs within seconds."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    fields = _target(body)
    if (
        repo.count_for_node(conn, node_id) >= MAX_PROBES_PER_NODE
        or repo.count_all(conn) >= MAX_PROBES
    ):
        raise ApiError("uptime.too_many", 409)
    probe_id = repo.create(conn, node_id, who.id, fields)
    _audit(conn, registry, who, "uptime.create", node_id, probe_id, kind=fields["kind"])
    return repo.view(repo.get(conn, probe_id))


@router.patch("/{probe_id}", summary="Change a probe")
def change_probe(
    node_id: str,
    body: ProbeChange,
    probe_id: str = ProbeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Rename, pause, resume or retime a probe."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    _probe_of(conn, node_id, probe_id)
    # Only what was sent; `expect_status` may be sent as null to go back to "any status below 400".
    fields = {
        k: (int(v) if isinstance(v, bool) else v)
        for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None or k == "expect_status"
    }
    if "name" in fields:
        fields["name"] = fields["name"].strip()
        if not fields["name"]:
            raise ApiError("uptime.invalid_target", 422)
    repo.update(conn, probe_id, fields)
    if fields.get("enabled") == 0:
        repo.reset_state(conn, probe_id)  # a paused probe says nothing about the site
    _audit(conn, registry, who, "uptime.update", node_id, probe_id, fields=",".join(sorted(fields)))
    return repo.view(repo.get(conn, probe_id))


@router.delete("/{probe_id}", status_code=204, summary="Delete a probe")
def delete_probe(
    node_id: str,
    probe_id: str = ProbeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove a probe and its history."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    _probe_of(conn, node_id, probe_id)
    repo.delete(conn, probe_id)
    _audit(conn, registry, who, "uptime.delete", node_id, probe_id)


@router.post("/{probe_id}/check", summary="Check a probe now")
async def check_now(
    node_id: str,
    request: Request,
    probe_id: str = ProbeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Run one check immediately and return the probe as it is afterwards."""
    authorise(conn, registry, who, node_id, "uptime.manage")
    row = _probe_of(conn, node_id, probe_id)
    now = time.monotonic()
    if now - _last_manual.get(probe_id, -1e9) < MIN_CHECK_GAP_SECONDS:
        raise ApiError("uptime.too_soon", 429)
    _last_manual[probe_id] = now
    if len(_last_manual) > 5000:
        for key in [k for k, t in _last_manual.items() if now - t > MIN_CHECK_GAP_SECONDS]:
            del _last_manual[key]
    _audit(conn, registry, who, "uptime.check", node_id, probe_id)
    verdict = await service.check(request.app, row)
    if verdict is None:
        raise ApiError("uptime.not_checked", 409)
    return repo.view(repo.get(conn, probe_id))
