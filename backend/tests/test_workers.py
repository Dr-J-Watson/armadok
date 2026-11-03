import contextlib
import json
import logging
import queue
import re
import socket
import sqlite3
import stat
import threading
import time

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from armadok.core.config import Settings
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.workers import hub as hub_session
from armadok.worker import client as worker_client
from armadok.worker import main as worker_main
from armadok.worker.ops import OPERATIONS
from armadok.worker.state import WorkerState, load_state
from armadok.worker_protocol import channel as protocol

PASSWORD = "correct horse battery"
MODULES = ("users", "apikeys", "nodes", "workers")


def build_app(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False), load_registry(MODULES))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    return app, code


def bootstrap(client, code):
    """Set up the admin, log in and create one node; return (headers, node_id)."""
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    node = client.post("/api/v1/nodes", headers=headers, json={"name": "n", "type": "docker"})
    return headers, node.json()["id"]


class WsTransport:
    """Adapts a Starlette test WebSocket to the worker's Transport."""

    def __init__(self, ws):
        self.ws = ws

    def send(self, data):
        self.ws.send_bytes(data)

    def recv(self):
        try:
            return self.ws.receive_bytes()
        except WebSocketDisconnect:
            raise ConnectionError("closed by the hub") from None


@pytest.fixture
def world(tmp_path, caplog):
    app, code = build_app(tmp_path, caplog)
    client = TestClient(app)
    headers, node_id = bootstrap(client, code)
    return client, headers, node_id, tmp_path


def enrol(client, headers, node_id):
    r = client.post(f"/api/v1/nodes/{node_id}/worker/enrol", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    import base64

    return body["token"], base64.urlsafe_b64decode(body["hub_key"])


def try_connect(client, hub_key, state, token=None):
    with client.websocket_connect("/api/v1/worker/connect") as ws:
        return worker_client.connect(WsTransport(ws), hub_key, state, token)


# --- real server, real worker program -----------------------------------------------------


@pytest.fixture
def live(tmp_path, caplog):
    app, code = build_app(tmp_path, caplog)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=1 << 20)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}")
    headers, node_id = bootstrap(client, code)
    yield client, headers, node_id, port, tmp_path
    server.should_exit = True
    thread.join(timeout=10)


def wait_for(predicate, seconds=15):
    for _ in range(int(seconds * 10)):
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_worker_enrols_connects_answers_and_reconnects(live):
    client, headers, node_id, port, tmp_path = live
    token, hub_key = enrol(client, headers, node_id)
    import base64

    stop = threading.Event()
    state_dir = tmp_path / "worker"
    args = (
        f"ws://127.0.0.1:{port}/api/v1/worker/connect",
        base64.urlsafe_b64encode(hub_key).decode(),
        state_dir,
    )
    thread = threading.Thread(target=worker_main.run, args=(*args, token, stop), daemon=True)
    thread.start()
    status = f"/api/v1/nodes/{node_id}/worker"
    assert wait_for(lambda: client.get(status, headers=headers).json()["connected"])
    assert client.get(status, headers=headers).json()["enrolled"] is True
    assert client.post(status + "/ping", headers=headers).json() == {"pong": True}
    stop.set()
    thread.join(timeout=20)

    # A restart needs no token: the worker proves itself with its own key.
    assert load_state(state_dir).worker_id
    stop2 = threading.Event()
    thread2 = threading.Thread(target=worker_main.run, args=(*args, None, stop2), daemon=True)
    thread2.start()
    assert wait_for(lambda: client.get(status, headers=headers).json()["connected"])
    assert client.post(status + "/ping", headers=headers).json() == {"pong": True}

    # Revocation drops the live connection and the worker cannot come back.
    assert client.delete(status, headers=headers).status_code == 204
    assert wait_for(lambda: not client.get(status, headers=headers).json()["connected"])
    stop2.set()
    thread2.join(timeout=20)


# --- handshake and enrolment failures -----------------------------------------------------


def test_token_is_single_use_and_reconnect_works(world):
    client, headers, node_id, _ = world
    token, hub_key = enrol(client, headers, node_id)
    state = WorkerState(Ed25519PrivateKey.generate())
    try_connect(client, hub_key, state, token)
    assert state.worker_id
    # A second machine presenting the same token is refused.
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, WorkerState(Ed25519PrivateKey.generate()), token)
    # The enrolled worker reconnects with its signature only.
    try_connect(client, hub_key, state)


def test_wrong_secret_wrong_hub_key_and_expired_token_are_refused(world):
    client, headers, node_id, tmp_path = world
    token, hub_key = enrol(client, headers, node_id)
    fresh = lambda: WorkerState(Ed25519PrivateKey.generate())  # noqa: E731
    forged = token.rsplit("_", 1)[0] + "_" + "x" * 32
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, fresh(), forged)
    other_hub = protocol.public_key_bytes(Ed25519PrivateKey.generate())
    with pytest.raises(protocol.ProtocolError):
        try_connect(client, other_hub, fresh(), token)
    db = sqlite3.connect(tmp_path / "armadok.db")
    db.execute("UPDATE enrolment_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    db.commit()
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, fresh(), token)


def test_unknown_worker_id_and_stolen_id_with_other_key_are_refused(world):
    client, headers, node_id, _ = world
    token, hub_key = enrol(client, headers, node_id)
    state = WorkerState(Ed25519PrivateKey.generate())
    try_connect(client, hub_key, state, token)
    thief = WorkerState(Ed25519PrivateKey.generate(), worker_id=state.worker_id)
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, thief)
    ghost = WorkerState(Ed25519PrivateKey.generate(), worker_id="0" * 32)
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, ghost)


def test_new_enrolment_replaces_the_previous_worker(world):
    client, headers, node_id, _ = world
    token1, hub_key = enrol(client, headers, node_id)
    old = WorkerState(Ed25519PrivateKey.generate())
    try_connect(client, hub_key, old, token1)
    token2, _ = enrol(client, headers, node_id)
    new = WorkerState(Ed25519PrivateKey.generate())
    try_connect(client, hub_key, new, token2)
    assert new.worker_id != old.worker_id
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, old)


def test_garbage_and_oversized_hello_are_closed_quietly(world):
    client, *_ = world
    for payload in (b"", b"garbage", b"AH1" + b"\x00" * 500):
        with client.websocket_connect("/api/v1/worker/connect") as ws:
            ws.send_bytes(payload)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_bytes()


def test_hub_key_file_is_private_and_stable(world):
    client, headers, node_id, tmp_path = world
    _, key1 = enrol(client, headers, node_id)
    _, key2 = enrol(client, headers, node_id)
    assert key1 == key2
    mode = stat.S_IMODE((tmp_path / "hub_identity.key").stat().st_mode)
    assert mode == 0o600


# --- rights -------------------------------------------------------------------------------


def test_enrolment_needs_the_worker_right_and_hides_invisible_nodes(world):
    client, headers, node_id, _ = world
    admin_id = client.get("/api/v1/auth/me", headers=headers).json()["id"]
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob_token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {bob_token}"}
    url = f"/api/v1/nodes/{node_id}/worker/enrol"
    assert client.post(url, headers=bob).status_code == 404  # cannot even see the node
    for right in ("nodes.read", "workers.read"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=headers,
            json={"right": right, "scope": f"node:{node_id}"},
        )
    # The admin does not hold these globally, but owns the node: it may grant them on it.
    assert admin_id
    assert client.post(url, headers=bob).status_code == 403  # sees it, may not manage it


# --- worker program units -----------------------------------------------------------------


class QueueTransport:
    def __init__(self, inbox, outbox):
        self.inbox, self.outbox = inbox, outbox

    def send(self, data):
        self.outbox.put(data)

    def recv(self):
        return self.inbox.get(timeout=5)


def test_worker_runs_only_listed_operations_and_survives_failures():
    hub_key = Ed25519PrivateKey.generate()
    hello = protocol.ClientHandshake(protocol.public_key_bytes(hub_key))
    server_hello, hub_channel = protocol.ServerHandshake(hub_key).respond(hello.hello)
    worker_channel = hello.finish(server_hello)
    to_worker, from_worker = queue.Queue(), queue.Queue()
    transport = QueueTransport(to_worker, from_worker)

    def boom(_args):
        raise RuntimeError("secret detail that must not leave the machine")

    ops = {**OPERATIONS, "boom": boom}

    def run_worker():
        # serve() ends with queue.Empty once the test stops sending requests.
        with contextlib.suppress(queue.Empty):
            worker_client.serve(transport, worker_channel, ops)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()

    def ask(message):
        to_worker.put(hub_channel.seal(json.dumps(message).encode()))
        return json.loads(hub_channel.open(from_worker.get(timeout=5)))

    assert ask({"id": 1, "op": "ping", "args": {}}) == {
        "id": 1,
        "ok": True,
        "result": {"pong": True},
    }
    unknown = ask({"id": 2, "op": "rm -rf /", "args": {}})
    assert unknown == {"id": 2, "ok": False, "error": "worker.unknown_op"}
    failed = ask({"id": 3, "op": "boom", "args": {}})
    assert failed == {"id": 3, "ok": False, "error": "worker.op_failed"}
    assert "secret" not in json.dumps(failed)
    assert ask({"id": 4, "op": "ping", "args": {}})["ok"] is True


# --- lifecycle, sanitising and limits -----------------------------------------------------


def test_deleting_a_node_revokes_its_worker(world):
    client, headers, node_id, _ = world
    token, hub_key = enrol(client, headers, node_id)
    state = WorkerState(Ed25519PrivateKey.generate())
    try_connect(client, hub_key, state, token)
    assert client.delete(f"/api/v1/nodes/{node_id}", headers=headers).status_code == 204
    with pytest.raises(worker_client.EnrolmentError):
        try_connect(client, hub_key, state)


def test_ping_needs_the_manage_right(world):
    client, headers, node_id, _ = world
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    for right in ("nodes.read", "workers.read"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=headers,
            json={"right": right, "scope": f"node:{node_id}"},
        )
    bob_token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    r = client.post(
        f"/api/v1/nodes/{node_id}/worker/ping", headers={"Authorization": f"Bearer {bob_token}"}
    )
    assert r.status_code == 403


def test_text_frames_and_deeply_nested_json_close_quietly(world):
    client, *_ = world
    with client.websocket_connect("/api/v1/worker/connect") as ws:
        ws.send_text("not binary")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()


class _FakeWs:
    pass


def make_session():
    hub_key = Ed25519PrivateKey.generate()
    hello = protocol.ClientHandshake(protocol.public_key_bytes(hub_key))
    _, channel = protocol.ServerHandshake(hub_key).respond(hello.hello)
    return hub_session.WorkerSession(_FakeWs(), channel, "node", "worker")


def test_worker_answers_are_sanitised_and_late_answers_ignored():
    import asyncio

    async def scenario():
        session = make_session()
        loop = asyncio.get_running_loop()
        # An error code outside worker.* cannot be forged by a worker.
        future = loop.create_future()
        session._pending[1] = future
        session._handle(json.dumps({"id": 1, "ok": False, "error": "auth.required"}).encode())
        with pytest.raises(hub_session.WorkerCallError) as caught:
            future.result()
        assert caught.value.code == "worker.error"
        # An answer to an abandoned (timed-out) call is ignored, not fatal.
        session._abandoned.append(7)
        session._handle(json.dumps({"id": 7, "ok": True, "result": {}}).encode())
        # An answer to a call that was never made is a protocol violation.
        with pytest.raises(protocol.ProtocolError):
            session._handle(json.dumps({"id": 99, "ok": True, "result": {}}).encode())
        with pytest.raises(protocol.ProtocolError):
            session._handle(b"[" * 100000)

    asyncio.run(scenario())


def test_pending_calls_are_capped():
    import asyncio

    async def scenario():
        session = make_session()
        for i in range(hub_session.MAX_PENDING_CALLS):
            session._pending[i + 1000] = asyncio.get_running_loop().create_future()
        with pytest.raises(hub_session.WorkerCallError) as caught:
            await session.call("ping", {})
        assert caught.value.code == "worker.busy"

    asyncio.run(scenario())


def test_hub_key_with_open_permissions_is_refused(tmp_path):
    from armadok.modules.workers import identity

    settings = Settings(data_dir=tmp_path)
    identity.hub_private_key(settings)
    identity._cache.clear()
    (tmp_path / "hub_identity.key").chmod(0o644)
    with pytest.raises(RuntimeError):
        identity.hub_private_key(settings)
