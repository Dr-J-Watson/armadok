"""The link between Traefik and DNS: which hosts the sites use, and whether the DNS has them.

Hosts come from the relations map (routers of files AND of Docker labels, whatever the viewer may
see), so this report and the map always agree. Each host is matched to the viewer's most specific
verified domain, then checked against that domain's records when the provider can list them.

Reading a zone costs provider requests, so results are kept for a short while and the hub only
resolves names that belong to a domain the viewer can use (it is not a general DNS probe).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import sqlite3
import time
from typing import Any

from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.dns import base, repository, service
from armadok.modules.dns.routes import RecordIn
from armadok.modules.relations.service import build_graph

RESOLVE_TIMEOUT_SECONDS = 3
RESOLVE_CONCURRENCY = 8
REPORT_DEADLINE_SECONDS = 45
CACHE_SECONDS = 30
MAX_HOSTS = 200


def relative_name(host: str, zone: str) -> str:
    """Return `host` relative to `zone` ("" for the apex)."""
    return "" if host == zone else host[: -(len(zone) + 1)]


def covers(record_name: str, wanted: str) -> bool:
    """Return True if a record named `record_name` answers for the relative name `wanted`.

    A wildcard stands for exactly one label, like in DNS: `*` covers `blog` but not `a.b`.
    """
    if record_name == wanted:
        return True
    if record_name == "*":
        return wanted != "" and "." not in wanted
    if record_name.startswith("*."):
        head, _, tail = wanted.partition(".")
        return bool(head) and tail == record_name[2:]
    return False


def best_domain(
    host: str, domains: list[sqlite3.Row], verified_ids: set[str] | None = None
) -> sqlite3.Row | None:
    """Return the domain that holds `host`: the longest matching name, preferring verified ones.

    A domain with no provider account is only a name anyone could have typed; it must not be
    able to shadow a real zone, so verified domains win when there are any.
    """
    matching = [d for d in domains if host == d["name"] or host.endswith("." + d["name"])]
    if verified_ids is not None:
        proven = [d for d in matching if d["id"] in verified_ids]
        matching = proven or matching
    return max(matching, key=lambda d: len(d["name"]), default=None)


def _public_only(addresses: list[str]) -> list[str]:
    """Keep globally routable addresses: private ones say nothing the viewer needs."""
    keep = []
    for address in addresses:
        try:
            if ipaddress.ip_address(address.split("%")[0]).is_global:
                keep.append(address)
        except ValueError:
            continue
    return keep


async def _resolves(host: str, slots: asyncio.Semaphore) -> list[str]:
    async with slots:
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM),
                RESOLVE_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError):
            return []
    return _public_only(sorted({info[4][0] for info in infos}))


def _usable(registry: Registry, conn: sqlite3.Connection, who: Principal) -> list[sqlite3.Row]:
    return [d for d in repository.list_domains(conn) if service.access(registry, conn, who, d)]


def _verified_ids(
    registry: Registry, conn: sqlite3.Connection, domains: list[sqlite3.Row]
) -> set[str]:
    return {d["id"] for d in domains if service.verified(registry, conn, d)}


async def _records(
    app: Any, registry: Registry, conn: sqlite3.Connection, domain: sqlite3.Row
) -> list[base.Record] | str:
    """Return a zone's records (cached briefly), or a status string if they cannot be read."""
    if "records" not in service.capabilities(registry, conn, domain):
        return "unmanaged"
    cache = app.state.__dict__.setdefault("dns_cache", {})
    cached = cache.get(domain["id"])
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        provider = service.open_provider(app, registry, conn, domain)
        found = await provider.list_records(domain["name"])
    except (base.ProviderError, ApiError):
        return "error"
    cache[domain["id"]] = (time.monotonic(), found)
    return found


async def node_report(
    app: Any, registry: Registry, conn: sqlite3.Connection, who: Principal, node: dict
) -> list[dict[str, Any]]:
    """Return one entry per host used by the node's sites: its domain, DNS status, resolution."""
    graph = await build_graph(app, registry, who, node["id"], node["type"])
    hosts = sorted({o.id for o in graph.objects.values() if o.type == "domain"})[:MAX_HOSTS]
    domains = _usable(registry, conn, who)
    proven = _verified_ids(registry, conn, domains)
    slots = asyncio.Semaphore(RESOLVE_CONCURRENCY)

    async def one(host: str) -> dict[str, Any]:
        domain = best_domain(host, domains, proven) if base.is_domain(host) else None
        entry: dict[str, Any] = {
            "host": host,
            "domain_id": None,
            "status": "no_domain",
            "resolves": [],
        }
        if domain is None:
            return entry  # not one of the viewer's domains: nothing is looked up for it
        entry["domain_id"] = domain["id"]
        found = await _records(app, registry, conn, domain)
        if isinstance(found, str):
            entry["status"] = found
        else:
            wanted = relative_name(host, domain["name"])
            present = any(
                r.type in ("A", "AAAA", "CNAME") and covers(r.name, wanted) for r in found
            )
            entry["status"] = "present" if present else "missing"
        entry["resolves"] = await _resolves(host, slots)
        return entry

    try:
        async with asyncio.timeout(REPORT_DEADLINE_SECONDS):
            return list(await asyncio.gather(*(one(h) for h in hosts)))
    except TimeoutError:
        raise ApiError("dns.unreachable", 502) from None


async def plan_missing(
    app: Any,
    registry: Registry,
    conn: sqlite3.Connection,
    who: Principal,
    node: dict,
    host: str,
    record_type: str | None,
    target: str | None,
) -> tuple[sqlite3.Row, RecordIn]:
    """Work out the record a host needs: `(domain, record)`. Nothing is written here.

    The host must be one the node's sites really use (a `dns.read` holder on one node cannot
    create records for another node's names). Default target: the zone's apex address.
    """
    hosts = {
        o.id
        for o in (await build_graph(app, registry, who, node["id"], node["type"])).objects.values()
        if o.type == "domain"
    }
    if host not in hosts:
        raise NotFound()
    domains = _usable(registry, conn, who)
    domain = best_domain(host, domains, _verified_ids(registry, conn, domains))
    if domain is None:
        raise NotFound()
    if "write" not in service.capabilities(registry, conn, domain):
        raise ApiError("dns.not_supported", 409)
    if target is None:
        found = await _records(app, registry, conn, domain)
        apex = (
            [r for r in found if r.name == "" and r.type in ("A", "AAAA")]
            if isinstance(found, list)
            else []
        )
        if not apex:
            raise ApiError("dns.no_default_target", 409)
        record_type, target = apex[0].type, apex[0].target
    try:
        record = RecordIn(
            type=record_type or "A",
            name=relative_name(host, domain["name"]),
            target=target,
            ttl=3600,
        )
    except ValueError:
        raise ApiError("dns.invalid_record", 422) from None
    return domain, record
