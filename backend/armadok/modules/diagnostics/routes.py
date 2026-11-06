"""Read-only checks that guide the first start.

`/system/status` says how the hub itself is set up (secure cookies, HTTPS, the key that
encrypts secrets, trust in the reverse proxy) so an administrator can fix a weak set-up
before relying on it. `/nodes/{id}/readiness` asks the node's worker what it is ready for
(Docker, projects folder, Traefik folder, certificates file). Neither changes anything,
and neither returns a path, a secret or a name from the node: only a state per capability.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time

from fastapi import APIRouter, Depends, Path, Request

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.workers.access import authorise
from armadok.modules.workers.routes import call_operation

router = APIRouter(tags=["diagnostics"])

NodeId = Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
CHECK_TIMEOUT_SECONDS = 15

# capability -> (worker operation, right needed to look at it)
CAPABILITIES: dict[str, tuple[str, str]] = {
    "docker": ("docker.info", "docker.read"),
    "projects": ("projects.list", "projects.read"),
    "traefik": ("traefik.files.list", "traefik.read"),
    "certificates": ("certs.list", "certs.read"),
}

# A worker's answer is untrusted: "ok" means it answered with the shape we know, nothing more.
EXPECTED_KEYS = {
    "docker": "version",
    "projects": "projects",
    "traefik": "files",
    "certificates": "certificates",
}
CACHE_SECONDS = 5  # a page that is reloaded or hammered asks the worker once, not every time
MAX_CONCURRENT_CHECKS = 2  # per node: it must not take every pending-call slot of its worker
_cache: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, str]]] = {}
_locks: dict[str, asyncio.Semaphore] = {}

# What a failure means, in words the interface can translate (never the raw error).
_STATES = {
    "worker.docker_unavailable": "unavailable",
    "worker.projects_not_configured": "not_configured",
    "worker.traefik_not_configured": "not_configured",
    "worker.acme_not_configured": "not_configured",
    "worker.not_connected": "worker_offline",
    "worker.op_not_for_node_type": "not_applicable",
    "worker.timeout": "timeout",
}


@router.get("/system/status", summary="How the hub is set up")
def system_status(
    request: Request,
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the hub's own set-up, for the first-start checklist. Needs `users.create`.

    `https` is what this request used as the server saw it. Behind a proxy that depends on
    its forwarded headers being trusted, which `proxy_trust` reports: `none`, `restricted`
    (named addresses) or `any` (everything: a weakness, flagged). A header sent by the client
    itself is never taken at its word.
    """
    who.require("users.create", registry.rights())
    settings = request.app.state.settings
    allowed = os.environ.get("UVICORN_FORWARDED_ALLOW_IPS", "").strip()
    return {
        "version": os.environ.get("ARMADOK_VERSION", "0.0.0"),
        "https": request.url.scheme == "https",
        "cookie_secure": bool(settings.cookie_secure),
        "encryption_key_set": bool(settings.encryption_key),
        "proxy_trust": "none" if not allowed else ("any" if "*" in allowed else "restricted"),
        "local_node_enabled": bool(settings.local_node),
    }


async def _check(
    request: Request, registry: Registry, node_id: str, name: str, op: str, actor: str
) -> str:
    try:
        result = await asyncio.wait_for(
            call_operation(request.app, registry, node_id, op, {}, actor), CHECK_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return "timeout"
    except ApiError as error:
        return _STATES.get(error.code, "error")
    return "ok" if isinstance(result, dict) and EXPECTED_KEYS[name] in result else "error"


@router.get("/nodes/{node_id}/readiness", summary="What a node is ready for")
async def readiness(
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return a state per capability of the node.

    The states are `ok`, `not_configured`, `unavailable`, `worker_offline`, `not_applicable`,
    `timeout`, `error` and `no_right`. A capability is only looked at if you hold the right to
    read it. The answer is kept a few seconds and the worker is asked by two checks at a time
    at most, so reloading the page cannot flood the node.
    """
    node = authorise(conn, registry, who, node_id, "nodes.read")
    catalogue = registry.rights()
    scope = f"node:{node_id}"
    wanted = tuple(
        name for name, (_, right) in CAPABILITIES.items() if who.has(right, catalogue, scope)
    )
    actor = who.id if who.kind == "session" else ""
    key = (node_id, wanted)
    async with _locks.setdefault(node_id, asyncio.Semaphore(1)):  # one run per node at once
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            found = cached[1]
        else:
            gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

            async def limited(name: str) -> str:
                async with gate:
                    return await _check(
                        request, registry, node_id, name, CAPABILITIES[name][0], actor
                    )

            states = await asyncio.gather(*(limited(n) for n in wanted))
            found = dict(zip(wanted, states, strict=True))
            _cache[key] = (time.monotonic(), found)
            if len(_cache) > 500:
                _cache.clear()
    return {"node": node["name"], "checks": {n: found.get(n, "no_right") for n in CAPABILITIES}}
