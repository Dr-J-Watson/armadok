"""Worker endpoints: enrolment, status, revocation, test call, and the WebSocket workers dial.

Operators manage a node's worker through the REST routes (node-scoped rights). The worker
itself connects to `/worker/connect`; there it proves who it is inside the encrypted channel,
so that route needs no login and answers every failure the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.events import emit
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.workers import identity, repository
from armadok.modules.workers.hub import (
    WorkerCallError,
    WorkerHub,
    WorkerSession,
    receive_frame,
)
from armadok.worker_protocol import channel as protocol

router = APIRouter(tags=["workers"])
log = logging.getLogger("armadok.workers")

# Worker error codes that are the caller's situation, not a fault of the worker.
WORKER_ERROR_STATUS = {
    "worker.busy": 429,
    "worker.conflict": 409,
    "worker.not_found": 404,
    "worker.invalid_path": 422,
    "worker.forbidden": 422,
    "worker.too_large": 413,
    "worker.not_text": 415,
    "worker.projects_not_configured": 409,
    "worker.traefik_not_configured": 409,
}
HANDSHAKE_TIMEOUT_SECONDS = 10
MAX_HELLO_BYTES = 200
MAX_AUTH_BYTES = 2048
_HEX = re.compile(r"[0-9a-f]+", re.ASCII)


def get_hub(app: Any) -> WorkerHub:
    """Return the app's connection registry, creating it on first use."""
    if getattr(app.state, "worker_hub", None) is None:
        app.state.worker_hub = WorkerHub()
    return app.state.worker_hub


def _scope(node_id: str) -> str:
    return f"node:{node_id}"


def _visible_node(
    conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str
) -> dict:
    """Return the node if the caller may read it, else raise `NotFound` (no leak)."""
    node = nodes_repo.get_node(conn, node_id)
    if node is None or not who.has("nodes.read", registry.rights(), _scope(node_id)):
        raise NotFound()
    return node


def _audit(conn, registry, actor, event, node_id):
    audit.record(conn, registry.audit_events(), actor, event, object_type="node", object_id=node_id)


# --- operator routes ----------------------------------------------------------------------


@router.post("/nodes/{node_id}/worker/enrol", summary="Create a one-time enrolment token")
def create_enrolment(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, str]:
    """Return a token valid 15 minutes and the hub key the worker must pin.

    The token is shown only here. It lets one machine become this node's worker; enrolling
    again later replaces the previous worker.
    """
    _visible_node(conn, registry, who, node_id)
    who.require("workers.manage", registry.rights(), _scope(node_id))
    token, expires = repository.create_enrolment(conn, node_id, protocol.enrolment_public_hex)
    _audit(conn, registry, who.actor, "worker.enrolment_created", node_id)
    return {
        "token": token,
        "expires_at": expires,
        "hub_key": identity.hub_public_key_b64(request.app.state.settings),
    }


@router.get("/nodes/{node_id}/worker", summary="Worker status of a node")
def worker_status(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return whether a worker is enrolled and whether it is connected right now."""
    _visible_node(conn, registry, who, node_id)
    who.require("workers.read", registry.rights(), _scope(node_id))
    worker = repository.get_worker_of_node(conn, node_id)
    return {
        "enrolled": worker is not None,
        "worker_id": worker["id"] if worker else None,
        "connected": get_hub(request.app).get(node_id) is not None,
        "last_seen_at": worker["last_seen_at"] if worker else None,
    }


@router.delete("/nodes/{node_id}/worker", status_code=204, summary="Revoke the worker")
async def revoke_worker(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Revoke the node's worker and cancel pending tokens; a live connection is dropped."""
    _visible_node(conn, registry, who, node_id)
    who.require("workers.manage", registry.rights(), _scope(node_id))
    if not repository.revoke_node_worker(conn, node_id):
        raise NotFound()
    # Audited before the connection is cut, so the record exists even if closing misbehaves.
    _audit(conn, registry, who.actor, "worker.revoked", node_id)
    await get_hub(request.app).disconnect_node(node_id)


@router.post("/nodes/{node_id}/worker/ping", summary="Check that the worker answers")
async def ping_worker(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Send the `ping` operation to the node's worker and return its answer."""
    _visible_node(conn, registry, who, node_id)
    # Managing right, not just reading: this makes the machine do something.
    who.require("workers.manage", registry.rights(), _scope(node_id))
    _audit(conn, registry, who.actor, "worker.ping", node_id)
    return await call_operation(request.app, registry, node_id, "ping", {})


async def call_operation(
    app: Any, registry: Registry, node_id: str, op: str, args: dict[str, Any]
) -> dict:
    """Run one operation on a node's worker. Other modules call this, never the socket.

    Only operations declared by a module (the closed list) can be sent.
    """
    declared = registry.worker_ops().get(op)
    if declared is None:
        raise ValueError(f"operation {op!r} is not declared by any module")
    conn = app.state.settings.open_db()
    try:
        node = nodes_repo.get_node(conn, node_id)
    finally:
        conn.close()
    if node is None:
        raise NotFound()
    if node["type"] not in declared.node_types:
        raise ApiError("worker.op_not_for_node_type", 409)
    session = get_hub(app).get(node_id)
    if session is None:
        raise ApiError("worker.not_connected", 409)
    try:
        return await session.call(op, args)
    except WorkerCallError as error:
        raise ApiError(error.code, WORKER_ERROR_STATUS.get(error.code, 502)) from None


# --- the connection workers dial ----------------------------------------------------------


@router.websocket("/worker/connect")
async def worker_connect(ws: WebSocket) -> None:
    """Accept a worker: handshake, authenticate inside the channel, then serve it."""
    await ws.accept()
    hub = get_hub(ws.app)
    if hub.preauth.locked():
        # Too many half-open connections: shed load before spending any cryptography.
        await ws.close(code=1013)
        return
    session: WorkerSession | None = None
    try:
        async with hub.preauth:
            session, ready = await asyncio.wait_for(_authenticate(ws), HANDSHAKE_TIMEOUT_SECONDS)
        await hub.attach(session)
        # Check again now that the session is registered: a revocation that ran before this
        # point is seen here, and one that runs later finds the session and closes it.
        if not await asyncio.to_thread(_still_valid, ws.app.state.settings, session):
            raise protocol.ProtocolError("worker revoked")
        await session.send_json(ready)
        await _announce(ws.app, session, "worker.connected")
        await session.run()
    except Exception as error:
        # Every failure ends the same way, without telling the peer why. The reason is kept
        # in the server log only (never any secret).
        log.info("worker connection closed: %s", type(error).__name__)
    finally:
        if session is not None:
            hub.detach(session)
            # A worker cut off on purpose (revoked, node deleted) is not an incident.
            if await asyncio.to_thread(_still_valid, ws.app.state.settings, session):
                await _announce(ws.app, session, "worker.disconnected")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1008), 2)


async def _announce(app: Any, session: WorkerSession, event: str) -> None:
    """Tell the people watching this node that its worker came or went."""
    conn = app.state.settings.open_db()
    try:
        node = nodes_repo.get_node(conn, session.node_id)
    finally:
        conn.close()
    try:
        await emit(
            app,
            "notify",
            event=event,
            node_id=session.node_id,
            params={"node": node["name"] if node else session.node_id},
        )
    except Exception:  # noqa: BLE001  (a notification problem must never drop the worker)
        log.exception("could not announce %s", event)


async def _authenticate(ws: WebSocket) -> tuple[WorkerSession, dict]:
    """Run the handshake and the worker's proof; return the session and its `ready` message."""
    settings = ws.app.state.settings
    registry: Registry = ws.app.state.registry
    server = protocol.ServerHandshake(identity.hub_private_key(settings))

    hello = await receive_frame(ws)
    if len(hello) > MAX_HELLO_BYTES:
        raise protocol.ProtocolError("hello too large")
    server_hello, channel = server.respond(hello)
    await ws.send_bytes(server_hello)

    frame = await receive_frame(ws)
    if len(frame) > MAX_AUTH_BYTES:
        raise protocol.ProtocolError("auth too large")
    try:
        message = json.loads(channel.open(frame))
    except RecursionError:
        raise protocol.ProtocolError("bad auth") from None
    if not isinstance(message, dict):
        raise protocol.ProtocolError("bad auth")

    # Database work and signature checks run off the event loop.
    node_id, worker_id = await asyncio.to_thread(
        _verify_worker, settings, registry, server, message
    )
    session = WorkerSession(ws, channel, node_id, worker_id)
    return session, {"type": "ready", "worker_id": worker_id, "node_id": node_id}


def _verify_worker(
    settings: Any, registry: Registry, server: protocol.ServerHandshake, message: dict
) -> tuple[str, str]:
    """Check an enrolment or a reconnection; return `(node_id, worker_id)` or raise."""
    conn = settings.open_db()
    try:
        if message.get("type") == "enroll":
            node_id, worker_id = _enrol(conn, registry, server.transcript, message)
        elif message.get("type") == "auth":
            node_id, worker_id = _reconnect(conn, server, message)
        else:
            raise protocol.ProtocolError("unknown auth type")
        repository.touch(conn, worker_id)
        return node_id, worker_id
    finally:
        conn.close()


def _still_valid(settings: Any, session: WorkerSession) -> bool:
    """Return True while the worker is not revoked and its node still exists."""
    conn = settings.open_db()
    try:
        return (
            repository.get_active_worker(conn, session.worker_id) is not None
            and nodes_repo.get_node(conn, session.node_id) is not None
        )
    finally:
        conn.close()


def _hex(value: Any, length: int | None = None) -> bytes:
    """Decode a lowercase hex string field, or raise `ProtocolError`."""
    if not isinstance(value, str) or not _HEX.fullmatch(value) or len(value) % 2:
        raise protocol.ProtocolError("bad field")
    if length is not None and len(value) != length * 2:
        raise protocol.ProtocolError("bad field length")
    return bytes.fromhex(value)


def _enrol(conn: sqlite3.Connection, registry: Registry, transcript: bytes, msg: dict):
    """Check an enrolment (token proof + key ownership) and register the worker."""
    token_id = msg.get("token_id")
    if not isinstance(token_id, str) or not re.fullmatch(r"[0-9a-f]{12}", token_id):
        raise protocol.ProtocolError("bad token id")
    public_key = _hex(msg.get("worker_pub"), 32)
    proof = _hex(msg.get("proof"), 64)
    signature = _hex(msg.get("sig"), 64)
    row = repository.get_open_enrolment(conn, token_id)
    if row is None:
        raise protocol.ProtocolError("token not usable")
    if not protocol.verify_enrolment_proof(row["key"], proof, transcript):
        raise protocol.ProtocolError("bad proof")
    if not protocol.verify_worker_signature(public_key, signature, b"enroll", transcript):
        raise protocol.ProtocolError("bad signature")
    if nodes_repo.get_node(conn, row["node_id"]) is None:
        raise protocol.ProtocolError("node is gone")
    worker_id = repository.enrol_worker(conn, token_id, row["node_id"], public_key.hex())
    if worker_id is None:
        raise protocol.ProtocolError("token already used")
    audit.record(
        conn,
        registry.audit_events(),
        f"worker:{worker_id}",
        "worker.enrolled",
        object_type="node",
        object_id=row["node_id"],
    )
    return row["node_id"], worker_id


def _reconnect(conn: sqlite3.Connection, server: protocol.ServerHandshake, msg: dict):
    """Check a known worker's signature over this connection."""
    worker_id = msg.get("worker_id")
    if worker_id != server.worker_id or not isinstance(worker_id, str):
        raise protocol.ProtocolError("worker id mismatch")
    signature = _hex(msg.get("sig"), 64)
    row = repository.get_active_worker(conn, worker_id)
    if row is None or not protocol.verify_worker_signature(
        bytes.fromhex(row["public_key"]), signature, b"auth", server.transcript
    ):
        raise protocol.ProtocolError("worker not accepted")
    if nodes_repo.get_node(conn, row["node_id"]) is None:
        raise protocol.ProtocolError("node is gone")
    return row["node_id"], worker_id


async def on_node_deleted(app: Any, node_id: str) -> None:
    """Revoke the worker of a deleted node and cut its connection."""
    conn = app.state.settings.open_db()
    try:
        repository.revoke_node_worker(conn, node_id)
    finally:
        conn.close()
    await get_hub(app).disconnect_node(node_id)
