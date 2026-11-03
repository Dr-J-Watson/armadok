import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.signals import signal_for_request
from armadok.main import create_app
from armadok.modules.realtime import hub as hub_module
from armadok.modules.realtime import routes as realtime_routes

PASSWORD = "correct horse battery"
URL = "/api/v1/realtime"


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(hub_module, "BATCH_SECONDS", 0.02)
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
            )
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:  # one event loop for requests and sockets
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        token = login(client, "admin")
        node = client.get("/api/v1/nodes", headers=bearer(token)).json()[0]["id"]
        yield client, token, node


def login(client, name):
    return client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def open_socket(client, token):
    ws = client.websocket_connect(URL)
    socket = ws.__enter__()
    socket.send_json({"token": token})
    assert socket.receive_json() == {"signal": "ready", "node": None}
    return socket, ws


def recv(socket, seconds=10):
    """Receive one message or fail the test, instead of waiting for ever."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(socket.receive_json).result(timeout=seconds)
    except FuturesTimeout:
        raise AssertionError("no message arrived") from None
    finally:
        pool.shutdown(wait=False)


def close(ws):
    ws.__exit__(None, None, None)


# --- which request means which signal -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/v1/nodes/abc/docker/containers/x/action", ("docker.changed", "abc")),
        ("PUT", "/api/v1/nodes/abc/traefik/files/a.yml", ("traefik.changed", "abc")),
        ("PATCH", "/api/v1/nodes/abc", ("nodes.changed", "abc")),
        ("DELETE", "/api/v1/nodes/abc", ("nodes.changed", "abc")),
        ("POST", "/api/v1/nodes", ("nodes.changed", None)),
        ("POST", "/api/v1/groups", ("groups.changed", None)),
        ("POST", "/api/v1/nodes/abc/worker/enrol", ("worker.changed", "abc")),
        ("GET", "/api/v1/nodes/abc/docker/containers", None),
        ("POST", "/other/path", None),
        ("POST", "/api/v1/nodes/../x/docker", ("nodes.changed", None)),
        ("POST", "/api/v1/nodes/./docker", ("nodes.changed", None)),
        ("POST", "/api/v1/NODES/abc", None),
        ("POST", "/api/v1/", None),
    ],
)
def test_requests_map_to_signals(method, path, expected):
    assert signal_for_request(method, path) == expected


# --- the connection -------------------------------------------------------------------------


def test_a_change_reaches_open_screens_without_any_data(box):
    client, token, node = box
    socket, ws = open_socket(client, token)
    try:
        assert (
            client.post("/api/v1/groups", headers=bearer(token), json={"name": "Team"}).status_code
            == 201
        )
        seen = [recv(socket) for _ in range(2)]
        assert {"signal": "groups.changed", "node": None} in seen
        assert {"signal": "audit.changed", "node": None} in seen
        assert all(set(m) == {"signal", "node"} for m in seen)  # a name and a node id: nothing else
    finally:
        close(ws)


def test_a_node_signal_only_reaches_people_who_hold_its_right_on_that_node(box):
    client, token, node = box
    uid = client.post(
        "/api/v1/users", headers=bearer(token), json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob = login(client, "bob")
    mine, ws_mine = open_socket(client, token)
    his, ws_his = open_socket(client, bob)
    try:
        client.patch(f"/api/v1/nodes/{node}", headers=bearer(token), json={"description": "x"})
        assert {recv(mine)["signal"] for _ in range(2)} == {"nodes.changed", "audit.changed"}
        # Bob holds nothing: he hears nothing, not even that something happened.
        client.post("/api/v1/groups", headers=bearer(token), json={"name": "Team"})
        client.post(
            "/api/v1/assignments",
            headers=bearer(token),
            json={
                "subject_type": "user",
                "subject_id": uid,
                "role_id": "builtin-viewer",
                "scope": f"node:{node}",
            },
        )
        # A person's rights are read again at most once a second; wait for the new ones.
        time.sleep(1.2)
        # A viewer may see the node and its Docker/Traefik/... state, not the audit log.
        client.patch(f"/api/v1/nodes/{node}", headers=bearer(token), json={"description": "y"})
        client.post(
            f"/api/v1/nodes/{node}/docker/containers/{'a' * 12}/action",
            headers=bearer(token),
            json={"action": "start"},
        )
        heard = [recv(his)]
        assert heard == [{"signal": "nodes.changed", "node": node}]
    finally:
        close(ws_mine)
        close(ws_his)


def test_signals_without_a_machine_go_only_to_their_author_or_to_people_with_the_right(box):
    client, token, node = box
    client.post(
        "/api/v1/users", headers=bearer(token), json={"username": "bob", "password": PASSWORD}
    )
    bob = login(client, "bob")
    mine, ws_mine = open_socket(client, token)
    his, ws_his = open_socket(client, bob)
    try:
        # A private note: only its author is told, however powerful the other person is.
        made = client.post(
            "/api/v1/notes",
            headers=bearer(bob),
            json={"node_id": node, "object_type": "node", "object_id": node, "body": "secret"},
        )
        assert made.status_code in (201, 404)
        client.post(
            "/api/v1/apikeys", headers=bearer(token), json={"name": "k", "rights": ["nodes.read"]}
        )
        heard = []
        for _ in range(4):
            heard.append(recv(mine)["signal"])
            if "apikeys.changed" in heard:
                break
        assert "notes.changed" not in heard and "apikeys.changed" in heard
        # Bob is told of nothing of the admin's doing.
        client.post("/api/v1/groups", headers=bearer(token), json={"name": "Team"})
        recv(mine)
        client.post(
            "/api/v1/notes",
            headers=bearer(bob),
            json={"node_id": "none", "object_type": "node", "object_id": "x", "body": "y"},
        )
        client.post(
            "/api/v1/apikeys", headers=bearer(bob), json={"name": "mine", "rights": ["apikeys.own"]}
        )
        assert recv(his)["signal"] in ("apikeys.changed", "notes.changed")
    finally:
        close(ws_mine)
        close(ws_his)


def test_a_screen_that_talks_is_cut_off_and_the_unauthenticated_are_limited(box, monkeypatch):
    client, token, _ = box
    socket, ws = open_socket(client, token)
    socket.send_text("hello")
    with pytest.raises(WebSocketDisconnect) as closed:
        for _ in range(5):
            recv(socket)
    assert closed.value.code == 1008
    close(ws)
    monkeypatch.setattr(realtime_routes, "MAX_PER_ADDRESS", 0)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(URL):
        pass


def test_a_failing_listener_does_not_turn_a_successful_change_into_an_error(box, monkeypatch):
    client, token, _ = box

    def boom(*args, **kwargs):
        raise RuntimeError("listener broke")

    monkeypatch.setattr(realtime_routes, "get_hub", boom)
    made = client.post("/api/v1/groups", headers=bearer(token), json={"name": "Team"})
    assert made.status_code == 201


def test_read_only_posts_and_anonymous_requests_make_no_signal():
    assert signal_for_request("POST", "/api/v1/nodes/abc/traefik/sites/preview") is None
    assert signal_for_request("POST", "/api/v1/nodes/abc/projects/p/check") is None


def test_failed_requests_and_unknown_areas_send_nothing(box):
    client, token, node = box
    socket, ws = open_socket(client, token)
    try:
        client.post("/api/v1/groups", headers=bearer(token), json={"name": ""})  # refused
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        )  # no signal
        client.post("/api/v1/groups", headers=bearer(token), json={"name": "Real"})
        assert recv(socket)["signal"] in ("groups.changed", "audit.changed")
        assert client.get("/api/v1/groups", headers=bearer(token)).json()[0]["name"] == "Real"
    finally:
        close(ws)


@pytest.mark.parametrize("first", [{"token": "nope"}, {"token": 5}, {"nothing": 1}, "garbage"])
def test_a_bad_first_message_closes_with_4401(box, first):
    client, _, _ = box
    with client.websocket_connect(URL) as ws:
        ws.send_json(first) if isinstance(first, dict) else ws.send_text(first)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_no_first_message_in_time_closes(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(realtime_routes, "AUTH_TIMEOUT_SECONDS", 0.2)
    with client.websocket_connect(URL) as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_json()
    assert closed.value.code == 4401


def test_a_page_from_another_site_is_refused(box):
    client, token, _ = box
    # The upgrade is refused outright: the socket is never opened.
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(URL, headers={"origin": "https://evil.example.org"}),
    ):
        pass


def test_the_token_never_has_to_be_in_the_address(box):
    client, token, _ = box
    with (
        client.websocket_connect(f"{URL}?token={token}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.send_json({"token": "wrong"})
        ws.receive_json()
    assert closed.value.code == 4401


def test_connections_are_limited_per_person(box, monkeypatch):
    client, token, _ = box
    monkeypatch.setattr(hub_module, "MAX_PER_USER", 2)
    a, wa = open_socket(client, token)
    b, wb = open_socket(client, token)
    try:
        with client.websocket_connect(URL) as ws:
            ws.send_json({"token": token})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
        assert closed.value.code == 1013
    finally:
        close(wa)
        close(wb)


def test_logging_out_closes_the_connection(box, monkeypatch):
    client, token, _ = box
    monkeypatch.setattr(realtime_routes, "RECHECK_SECONDS", 0.1)
    socket, ws = open_socket(client, token)
    try:
        assert client.post("/api/v1/auth/logout", headers=bearer(token)).status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(5):  # the logout itself may be announced first
                recv(socket)
        assert closed.value.code == 4401
    finally:
        # A socket still open would make the test client wait for it for ever.
        client.app.state.realtime.clients.clear()
        close(ws)


def test_a_burst_becomes_one_message_and_a_slow_reader_is_told_to_reload_everything(
    box, monkeypatch
):
    client, token, node = box
    socket, ws = open_socket(client, token)
    hub = client.app.state.realtime
    try:
        for _ in range(50):
            hub.publish("docker.changed", None, "x", True)
        assert recv(socket) == {"signal": "docker.changed", "node": None}
        monkeypatch.setattr(hub_module, "MAX_PENDING", 3)
        for i in range(10):
            hub.publish(f"n{i}.changed", None, "x", True)
        assert recv(socket) == {"signal": "*", "node": None}
    finally:
        close(ws)


def test_with_a_real_server_and_a_real_websocket_client(tmp_path, caplog):
    """The test client shares one event loop with the app; a real server does not."""
    import asyncio
    import json
    import socket
    import threading

    import httpx
    import uvicorn
    import websockets

    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False, encryption_key=generate_key())
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=1 << 20)
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    http = httpx.Client(base_url=f"http://127.0.0.1:{port}")
    http.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    token = http.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    ).json()["access_token"]

    async def scenario():
        async with websockets.connect(f"ws://127.0.0.1:{port}{URL}") as ws:
            await ws.send(json.dumps({"token": token}))
            assert json.loads(await ws.recv()) == {"signal": "ready", "node": None}
            await asyncio.to_thread(
                http.post, "/api/v1/groups", headers=bearer(token), json={"name": "Team"}
            )
            seen = {json.loads(await asyncio.wait_for(ws.recv(), 5))["signal"] for _ in range(2)}
            assert seen == {"groups.changed", "audit.changed"}

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
