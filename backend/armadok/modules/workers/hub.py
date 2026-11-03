"""Live worker connections held by the hub, and calls to them.

A `WorkerSession` wraps one authenticated, encrypted connection. Operations are sent only
if they belong to the closed list the modules declared; whatever a worker answers is treated
as untrusted (type, size, shape and error codes are checked before anything uses them).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from armadok.worker_protocol.channel import Channel, ProtocolError
from armadok.worker_protocol.error_codes import WORKER_ERROR_CODES

log = logging.getLogger("armadok.workers")

CALL_TIMEOUT_SECONDS = 30
SEND_TIMEOUT_SECONDS = 10
CLOSE_TIMEOUT_SECONDS = 2
MAX_PENDING_CALLS = 32
# How many timed-out call ids we remember, so their late answers are ignored, not fatal.
ABANDONED_MEMORY = 256
# Connections that have not finished authenticating, across all workers.
MAX_PREAUTH_CONNECTIONS = 64


class WorkerCallError(Exception):
    """Raised when a call to a worker fails; `code` is a stable, translatable error code."""

    def __init__(self, code: str) -> None:
        """Keep the error code."""
        super().__init__(code)
        self.code = code


async def receive_frame(ws: WebSocket) -> bytes:
    """Receive one binary message; anything else (text, close) ends the connection."""
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    data = message.get("bytes")
    if data is None:
        raise ProtocolError("binary frames only")
    return data


class WorkerSession:
    """One connected worker."""

    def __init__(self, ws: WebSocket, channel: Channel, node_id: str, worker_id: str) -> None:
        """Wrap an authenticated connection."""
        self.ws = ws
        self.channel = channel
        self.node_id = node_id
        self.worker_id = worker_id
        self._pending: dict[int, asyncio.Future] = {}
        self._abandoned: collections.deque[int] = collections.deque(maxlen=ABANDONED_MEMORY)
        self._next_id = 1
        # Sealing and sending must not interleave: counters have to leave in order.
        self._send_lock = asyncio.Lock()

    async def send_json(self, message: dict[str, Any]) -> None:
        """Encrypt and send one message, giving up if the peer stops reading."""
        async with self._send_lock:
            frame = self.channel.seal(json.dumps(message).encode())
            await asyncio.wait_for(self.ws.send_bytes(frame), SEND_TIMEOUT_SECONDS)

    async def call(self, op: str, args: dict[str, Any], timeout: float = CALL_TIMEOUT_SECONDS):
        """Ask the worker to run `op`; return its result dict or raise `WorkerCallError`."""
        if len(self._pending) >= MAX_PENDING_CALLS:
            raise WorkerCallError("worker.busy")
        call_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        try:
            await self.send_json({"id": call_id, "op": op, "args": args})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise WorkerCallError("worker.timeout") from None
        finally:
            self._pending.pop(call_id, None)
            if not future.done() or future.cancelled():
                # A late answer to this call must not be mistaken for a protocol violation.
                self._abandoned.append(call_id)

    async def run(self) -> None:
        """Read worker messages until the connection ends; resolve pending calls."""
        try:
            while True:
                self._handle(self.channel.open(await receive_frame(self.ws)))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(WorkerCallError("worker.disconnected"))

    def _handle(self, plaintext: bytes) -> None:
        """Validate one answer from the worker and hand it to the waiting call."""
        try:
            message = json.loads(plaintext)
        except (ValueError, RecursionError):
            raise ProtocolError("answer is not JSON") from None
        if not isinstance(message, dict) or type(message.get("id")) is not int:
            raise ProtocolError("malformed answer")
        call_id = message["id"]
        future = self._pending.get(call_id)
        if future is None:
            if call_id in self._abandoned:
                return
            # An answer to a call we never made: the peer is confused or hostile.
            raise ProtocolError("unexpected answer")
        if future.done():
            return
        if message.get("ok") is True and isinstance(message.get("result"), dict):
            future.set_result(message["result"])
            return
        code = message.get("error")
        if not isinstance(code, str) or code not in WORKER_ERROR_CODES:
            code = "worker.error"
        future.set_exception(WorkerCallError(code))


class WorkerHub:
    """The set of currently connected workers, one per node."""

    def __init__(self) -> None:
        """Start with no connections."""
        self._sessions: dict[str, WorkerSession] = {}
        self.preauth = asyncio.Semaphore(MAX_PREAUTH_CONNECTIONS)

    def get(self, node_id: str) -> WorkerSession | None:
        """Return the live session of a node, if connected."""
        return self._sessions.get(node_id)

    async def attach(self, session: WorkerSession) -> None:
        """Register a session; a previous one for the same node is closed (it was replaced).

        There is no `await` between looking up the old session and registering the new one,
        so a concurrent revocation always sees one or the other.
        """
        old = self._sessions.get(session.node_id)
        self._sessions[session.node_id] = session
        if old is not None:
            if old.worker_id == session.worker_id:
                log.warning("worker %s connected twice (copied identity?)", session.worker_id)
            await _close_quietly(old.ws)

    def detach(self, session: WorkerSession) -> None:
        """Forget a session, unless a newer one already replaced it."""
        if self._sessions.get(session.node_id) is session:
            del self._sessions[session.node_id]

    async def disconnect_node(self, node_id: str) -> None:
        """Drop the worker of a node (revocation, node deletion)."""
        session = self._sessions.pop(node_id, None)
        if session is not None:
            await _close_quietly(session.ws)


async def _close_quietly(ws: WebSocket) -> None:
    """Close a connection without letting a peer that stopped reading block the caller."""
    try:
        await asyncio.wait_for(ws.close(code=1008), CLOSE_TIMEOUT_SECONDS)
    except (RuntimeError, TimeoutError):
        pass
