"""Worker side of the hub connection: authenticate, then serve the hub's requests.

Written against a minimal `Transport` (send/recv of whole binary messages) so the same
code runs over a real WebSocket and in tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from armadok.worker.ops import Operation
from armadok.worker.state import WorkerState
from armadok.worker_protocol import channel as protocol

MAX_ERROR_CODE = "worker.op_failed"


class Transport(Protocol):
    """Whole-message binary transport (a WebSocket)."""

    def send(self, data: bytes) -> None:
        """Send one message."""

    def recv(self) -> bytes:
        """Receive one message, blocking."""


class EnrolmentError(Exception):
    """Raised when the hub did not accept this worker."""


def parse_enrol_token(token: str) -> tuple[str, str]:
    """Split `armtok_<id>_<secret>` into `(token_id, secret)`."""
    prefix, _, rest = token.partition("_")
    token_id, _, secret = rest.partition("_")
    if prefix != "armtok" or len(token_id) != 12 or not secret:
        raise EnrolmentError("invalid enrolment token")
    return token_id, secret


def connect(
    transport: Transport, hub_key: bytes, state: WorkerState, enrol_token: str | None = None
) -> protocol.Channel:
    """Run the handshake and prove identity; return the ready channel.

    A worker that already has an id reconnects with a signature; otherwise it enrols with
    the one-time token (it proves it knows the token without sending it). Updates
    `state.worker_id` after a successful enrolment: the caller must persist it.
    """
    handshake = protocol.ClientHandshake(hub_key, state.worker_id)
    transport.send(handshake.hello)
    channel = handshake.finish(transport.recv())
    transcript = handshake.transcript

    if state.worker_id:
        message: dict[str, Any] = {
            "type": "auth",
            "worker_id": state.worker_id,
            "sig": protocol.worker_signature(state.key, b"auth", transcript).hex(),
        }
    elif enrol_token:
        token_id, secret = parse_enrol_token(enrol_token)
        message = {
            "type": "enroll",
            "token_id": token_id,
            "worker_pub": protocol.public_key_bytes(state.key).hex(),
            "proof": protocol.enrolment_proof(secret, transcript).hex(),
            "sig": protocol.worker_signature(state.key, b"enroll", transcript).hex(),
        }
    else:
        raise EnrolmentError("not enrolled and no enrolment token given")

    transport.send(channel.seal(json.dumps(message).encode()))
    try:
        ready = json.loads(channel.open(transport.recv()))
    except (protocol.ProtocolError, ConnectionError) as error:
        raise EnrolmentError("the hub refused this worker") from error
    if not isinstance(ready, dict) or ready.get("type") != "ready":
        raise EnrolmentError("unexpected answer from the hub")
    if not state.worker_id:
        state.worker_id = str(ready["worker_id"])
    return channel


def serve(
    transport: Transport,
    channel: protocol.Channel,
    operations: dict[str, Operation],
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Answer the hub's requests until the connection ends.

    The hub is authenticated, but its requests are still checked: only listed operations
    run, and a failing operation never takes the worker down.
    """
    while not should_stop():
        request = json.loads(channel.open(transport.recv()))
        call_id = request.get("id") if isinstance(request, dict) else None
        if type(call_id) is not int:
            raise protocol.ProtocolError("malformed request")
        op, args = request.get("op"), request.get("args")
        handler = operations.get(op) if isinstance(op, str) else None
        if handler is None or not isinstance(args, dict):
            answer: dict[str, Any] = {"id": call_id, "ok": False, "error": "worker.unknown_op"}
        else:
            try:
                answer = {"id": call_id, "ok": True, "result": handler(args)}
            except Exception:
                # Details stay on the machine; the hub only gets a stable code.
                answer = {"id": call_id, "ok": False, "error": MAX_ERROR_CODE}
        transport.send(channel.seal(json.dumps(answer).encode()))
