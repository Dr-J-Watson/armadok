"""Collecting Traefik's access log and turning it into per-visitor statistics.

Visitor addresses are personal data. They are kept for a week; without the separate `read-ip`
right, every address is shown only as its network (`192.0.2.0/24` for IPv4, a /48 for IPv6),
and several addresses of one network are counted together.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from armadok.core.errors import ApiError
from armadok.modules.traffic import accesslog_repository as repo
from armadok.modules.traffic.repository import nodes_to_read
from armadok.modules.traffic.service import RANGES, STARTUP_DELAY_SECONDS
from armadok.modules.workers.routes import call_operation

log = logging.getLogger("armadok.traffic")
TICK_SECONDS = 30
RETRY_AFTER_ERROR = timedelta(minutes=10)
PURGE_EVERY_SECONDS = 600
MAX_TOP = 50
CHUNK_FULL = 2000  # the worker returns at most this many entries per call
CATCH_UP_SECONDS = 20
MAX_LOG = 100
_busy: set[str] = set()
_failed_at: dict[str, float] = {}


def canonical(address: str) -> str:
    """Return an address in its plain form: an IPv4 address in IPv6 dress becomes IPv4."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def mask(address: str) -> str:
    """Return the network an address belongs to (IPv4 /24, IPv6 /48) as text."""
    ip = ipaddress.ip_address(canonical(address))
    if isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour:
        # A 6to4 prefix IS the client's IPv4 address: a /48 would name one machine exactly.
        ip = ip.sixtofour
    return str(ipaddress.ip_network(f"{ip}/{24 if ip.version == 4 else 48}", strict=False))


def _printable(value: str) -> str:
    if not value.isprintable():
        raise ValueError("not printable")
    return value


class Entry(BaseModel):
    """One access-log line as the worker reports it (untrusted)."""

    model_config = ConfigDict(extra="ignore", strict=True)
    ts: int = Field(ge=0, le=4_000_000_000)
    ip: str = Field(max_length=45)
    host: str = Field(max_length=253)
    path: str = Field(max_length=300)
    method: str = Field(max_length=10)
    status: int = Field(ge=100, le=999)
    ms: int = Field(ge=0, le=3_600_000)
    bytes: int = Field(ge=0, le=10**12)
    router: str = Field(max_length=200)
    service: str = Field(max_length=200)

    @field_validator("host", "path", "method", "router", "service")
    @classmethod
    def printable(cls, value: str) -> str:
        """Text to show: no control characters."""
        return _printable(value)

    @field_validator("ip")
    @classmethod
    def an_address(cls, value: str) -> str:
        """Keep the canonical form of a real IP address."""
        return canonical(value)


class Chunk(BaseModel):
    """The worker's answer to `traffic.accesslog`."""

    model_config = ConfigDict(extra="ignore", strict=True)
    inode: int = Field(ge=0, lt=2**63)  # SQLite integers stop there; some filesystems exceed it
    offset: int = Field(ge=0)
    entries: list[Any] = Field(max_length=2000)


def _store(app: Any, node_id: str, expected: tuple[int, int] | None, chunk: Chunk) -> bool:
    """Keep the valid, recent entries of a chunk and move the position (one transaction).

    False if the position changed meanwhile (a purge): the chunk is then thrown away.
    """
    now = time.time()
    kept: list[dict[str, Any]] = []
    for raw in chunk.entries:
        try:
            entry = Entry.model_validate(raw)
        except ValidationError:
            continue  # one odd line from the worker does not sink the others
        if repo.RETENTION.total_seconds() >= now - entry.ts and entry.ts <= now + 3600:
            kept.append(entry.model_dump())
    conn = app.state.settings.open_db()
    try:
        done = repo.store(conn, node_id, expected, (chunk.inode, chunk.offset), kept)
        conn.commit()
        return done
    finally:
        conn.close()


async def collect_node(app: Any, node_id: str) -> str | None:
    """Read what was added to one machine's access log; return an error code or None.

    A busy site can write faster than one chunk per tick: while chunks come back full, the next
    one is read at once, within a time budget, so the collector does not fall ever further behind.
    """
    if node_id in _busy:
        return "traffic.busy"
    _busy.add(node_id)
    try:
        deadline = time.monotonic() + CATCH_UP_SECONDS
        while True:
            conn = app.state.settings.open_db()
            try:
                cursor = repo.get_cursor(conn, node_id)
            finally:
                conn.close()
            args = {"inode": cursor[0], "offset": cursor[1]} if cursor else {"inode": None}
            try:
                chunk = Chunk.model_validate(
                    await call_operation(
                        app, app.state.registry, node_id, "traffic.accesslog", args
                    )
                )
            except ApiError as error:
                return error.code
            except ValidationError:
                return "worker.invalid_answer"
            if not await asyncio.to_thread(_store, app, node_id, cursor, chunk):
                return None  # purged while reading: start again from the end next time
            if len(chunk.entries) < CHUNK_FULL or time.monotonic() > deadline:
                return None
    finally:
        _busy.discard(node_id)


def _since(range_name: str) -> int:
    return int(time.time()) - RANGES[range_name]


def visitors(
    conn: Any, node_id: str, range_name: str, service: str | None, with_ip: bool
) -> dict[str, Any]:
    """Return the top addresses (or networks), top paths and status split for a period."""
    since = _since(range_name)
    merged: dict[str, dict[str, Any]] = {}
    for row in repo.per_ip(conn, node_id, since, service):
        key = row["ip"] if with_ip else mask(row["ip"])
        item = merged.setdefault(
            key,
            {
                "ip": key,
                "requests": 0,
                "errors": 0,
                "bytes": 0,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            },
        )
        item["requests"] += row["requests"]
        item["errors"] += row["errors"] or 0
        item["bytes"] += row["bytes"] or 0
        item["first_seen"] = min(item["first_seen"], row["first_seen"])
        item["last_seen"] = max(item["last_seen"], row["last_seen"])
    top = sorted(merged.values(), key=lambda i: -i["requests"])[:MAX_TOP]
    for item in top:
        for field in ("first_seen", "last_seen"):
            item[field] = datetime.fromtimestamp(item[field], UTC).isoformat()
    return {
        "range": range_name,
        "masked": not with_ip,
        "visitors": top,
        "paths": [
            dict(r) | {"errors": r["errors"] or 0}
            for r in repo.per_path(conn, node_id, since, service, MAX_TOP)
        ],
        "classes": repo.per_class(conn, node_id, since, service),
        **repo.status(conn, node_id),
    }


def entries(
    conn: Any,
    node_id: str,
    range_name: str,
    *,
    service: str | None,
    klass: int | None,
    path: str | None,
    ip: str | None,
    with_ip: bool,
) -> list[dict[str, Any]]:
    """Return the newest entries that match the filters (addresses masked without `with_ip`).

    Raises `ApiError("traffic.invalid_address")` for an `ip` filter that is not an address.
    """
    if ip is not None:
        try:
            ip = canonical(ip)
        except ValueError:
            raise ApiError("traffic.invalid_address", 422) from None
    rows = repo.recent(
        conn,
        node_id,
        since=_since(range_name),
        service=service,
        klass=klass,
        path=path,
        ip=ip,
        limit=MAX_LOG,
    )
    return [
        {
            **dict(r),
            "ts": datetime.fromtimestamp(r["ts"], UTC).isoformat(),
            "ip": r["ip"] if with_ip else mask(r["ip"]),
        }
        for r in rows
    ]


async def watch_loop(app: Any) -> None:
    """Every half minute read the access logs of the machines that have one; trim every 10 min."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    last_purge = 0.0
    while True:
        try:
            conn = app.state.settings.open_db()
            try:
                nodes = [r["id"] for r in nodes_to_read(conn)]
            finally:
                conn.close()
            for node_id in nodes:
                if (
                    time.monotonic() - _failed_at.get(node_id, -1e9)
                    < RETRY_AFTER_ERROR.total_seconds()
                ):
                    continue
                try:
                    if await collect_node(app, node_id) is not None:
                        _failed_at[node_id] = time.monotonic()
                except Exception:  # noqa: BLE001  (one machine must not stop the others)
                    log.exception("could not read an access log")
            if time.monotonic() - last_purge > PURGE_EVERY_SECONDS:
                last_purge = time.monotonic()
                conn = app.state.settings.open_db()
                try:
                    repo.purge(conn)
                    conn.commit()
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001  (one bad round must not end the loop)
            log.exception("access log collection failed")
        await asyncio.sleep(TICK_SECONDS)
