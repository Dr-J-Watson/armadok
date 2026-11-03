import json
import logging
import re
import socketserver
import sqlite3
import struct
import threading
from http.server import BaseHTTPRequestHandler

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.worker import docker_control, docker_ops
from armadok.worker.ops import OperationError

PASSWORD = "correct horse battery"
MODULES = ("users", "apikeys", "nodes", "workers", "docker", "audit", "relations")
ID = "a" * 64
TTY_ID = "b" * 64
PROTECTED = "e" * 64
MISSING = "f" * 12
SEEN: list[tuple[str, str]] = []
STATUS: dict[str, int] = {}


def frame(kind: int, text: bytes) -> bytes:
    return struct.pack(">BxxxI", kind, len(text)) + text


class Fake(BaseHTTPRequestHandler):
    """A stand-in for the Docker daemon that records every request it gets."""

    def _answer(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self):
        SEEN.append((self.command, self.path))
        forced = STATUS.get(self.command)
        if forced:
            return self._answer(forced)
        path = self.path
        if path == "/images/json":
            image = {"Id": "sha256:" + "c" * 64, "RepoTags": ["nginx:1"], "Size": 5, "Created": 7}
            return self._answer(200, json.dumps([{**image, "Labels": {"s": "secret"}}]).encode())
        if path == "/networks":
            net = {"Id": "d" * 64, "Name": "demo_default", "Driver": "bridge", "Scope": "local"}
            return self._answer(200, json.dumps([{**net, "Options": {"k": "v"}}]).encode())
        if path == "/volumes":
            vol = {"Name": "data", "Driver": "local", "Mountpoint": "/var/lib/x", "CreatedAt": "t"}
            return self._answer(200, json.dumps({"Volumes": [vol]}).encode())
        match = re.fullmatch(r"/containers/([0-9a-f]+)/json", path)
        if match:
            if match.group(1) == MISSING:
                return self._answer(404)
            full = next((i for i in (ID, TTY_ID, PROTECTED) if i.startswith(match.group(1))), None)
            if full is None:
                return self._answer(404)
            config = {
                "Tty": full == TTY_ID,
                "Labels": {"armadok.protect": "true"} if full == PROTECTED else {},
            }
            return self._answer(200, json.dumps({"Id": full, "Config": config}).encode())
        if "/logs" in path and ID in path:
            return self._answer(200, frame(1, b"out line\n") + frame(2, b"err line\n"))
        if "/logs" in path and TTY_ID in path:
            return self._answer(200, b"x" * 200_000)
        if self.command in ("POST", "DELETE"):
            return self._answer(204)
        self._answer(404)

    do_GET = do_POST = do_DELETE = handle_any

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    SEEN.clear()
    STATUS.clear()
    path = str(tmp_path / "d.sock")
    server = Server(path, Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", path)
    yield path
    server.shutdown()


@pytest.mark.parametrize(
    ("action", "request_line"),
    [
        ("start", ("POST", f"/containers/{ID}/start")),
        ("stop", ("POST", f"/containers/{ID}/stop?t=10")),
        ("restart", ("POST", f"/containers/{ID}/restart?t=10")),
        ("remove", ("DELETE", f"/containers/{ID}?v=0&force=0&link=0")),
    ],
)
def test_each_action_sends_exactly_one_fixed_request(daemon, action, request_line):
    assert docker_control.container_action({"id": ID, "action": action}) == {"done": True}
    # The id is resolved first; then one fixed request (volumes are never removed, nothing forced).
    assert SEEN == [("GET", f"/containers/{ID}/json"), request_line]


def test_a_short_id_is_resolved_to_the_full_id_before_acting(daemon):
    docker_control.container_action({"id": ID[:12], "action": "start"})
    assert SEEN[-1] == ("POST", f"/containers/{ID}/start")


@pytest.mark.parametrize(
    "bad_id", ["../../etc", "A" * 20, "a" * 11, "a" * 65, "a b" * 6, 5, None, ["a" * 12]]
)
def test_bad_ids_never_reach_the_daemon(daemon, bad_id):
    with pytest.raises(OperationError) as caught:
        docker_control.container_action({"id": bad_id, "action": "start"})
    assert caught.value.code == "worker.invalid_path" and SEEN == []


@pytest.mark.parametrize("action", ["kill", "exec", "", None, ["start"], "Start"])
def test_unknown_actions_are_refused(daemon, action):
    with pytest.raises(OperationError) as caught:
        docker_control.container_action({"id": ID, "action": action})
    assert caught.value.code == "worker.invalid_path" and SEEN == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "worker.not_found"), (409, "worker.conflict"), (500, "worker.docker_error")],
)
def test_daemon_answers_become_codes(daemon, status, code):
    STATUS["POST"] = status
    with pytest.raises(OperationError) as caught:
        docker_control.container_action({"id": ID, "action": "stop"})
    assert caught.value.code == code


def test_already_started_is_not_an_error(daemon):
    STATUS["POST"] = 304
    assert docker_control.container_action({"id": ID, "action": "start"}) == {"done": True}


def test_the_worker_does_not_cut_its_own_container(daemon, monkeypatch):
    monkeypatch.setattr(docker_control, "own_container_id", lambda: ID)
    for action in ("stop", "restart", "remove"):
        with pytest.raises(OperationError) as caught:
            docker_control.container_action({"id": ID[:12], "action": action})
        assert caught.value.code == "worker.forbidden"
    assert all(method == "GET" for method, _ in SEEN)  # only inspected, never acted on
    docker_control.container_action({"id": ID, "action": "start"})  # starting is harmless
    docker_control.container_action({"id": TTY_ID, "action": "stop"})
    assert [m for m, _ in SEEN].count("POST") == 2


def test_protected_label_shields_a_container_whatever_the_hostname(daemon, monkeypatch):
    monkeypatch.setattr(docker_control, "own_container_id", lambda: None)
    with pytest.raises(OperationError) as caught:
        docker_control.container_action({"id": PROTECTED, "action": "remove"})
    assert caught.value.code == "worker.forbidden"
    assert all(method == "GET" for method, _ in SEEN)


def test_own_id_comes_from_the_mount_table(tmp_path, monkeypatch):
    table = tmp_path / "mountinfo"
    table.write_text(f"1 2 0:3 /docker/containers/{ID}/hostname /etc/hostname rw - ext4 x\n")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(table if p == "/proc/self/mountinfo" else p, *a, **k),
    )
    assert docker_control.own_container_id() == ID


def test_a_vanished_container_is_not_found(daemon):
    with pytest.raises(OperationError) as caught:
        docker_control.container_action({"id": MISSING, "action": "start"})
    assert caught.value.code == "worker.not_found"


def test_logs_shrink_instead_of_failing_when_lines_are_huge(daemon, monkeypatch):
    sizes = []
    real = docker_ops.docker_raw

    def raw(method, path, timeout=10):
        sizes.append(path)
        if "tail=200" in path:
            raise docker_ops.ResponseTooLarge("worker.docker_error")
        return real(method, path, timeout)

    monkeypatch.setattr(docker_ops, "docker_raw", raw)
    assert docker_ops.container_logs({"id": ID, "tail": 200})["output"] == "out line\nerr line\n"
    assert any("tail=50" in p for p in sizes)


def test_logs_join_both_streams_and_raw_tty_output_is_cut_to_the_tail(daemon):
    assert docker_ops.container_logs({"id": ID, "tail": 20})["output"] == "out line\nerr line\n"
    assert ("GET", f"/containers/{ID}/logs?stdout=1&stderr=1&tail=20") in SEEN
    big = docker_ops.container_logs({"id": TTY_ID})
    assert len(big["output"]) == docker_ops.MAX_LOG_BYTES and big["truncated"] is True


@pytest.mark.parametrize("tail", [0, 1001, -1, "5", True, 2.5])
def test_log_length_is_bounded(daemon, tail):
    with pytest.raises(OperationError):
        docker_ops.container_logs({"id": ID, "tail": tail})


def test_lists_keep_only_safe_fields(daemon):
    images = docker_ops.images_list({})["items"][0]
    assert images == {"id": "sha256:" + "c" * 64, "tags": ["nginx:1"], "size": 5, "created": 7}
    assert "Options" not in json.dumps(docker_ops.networks_list({}))
    volume = docker_ops.volumes_list({})["items"][0]
    assert volume == {"name": "data", "driver": "local", "created": "t"}  # no host path


# --- through the hub -----------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path, daemon, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
            ),
            load_registry(MODULES),
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    client = TestClient(app)
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    ).json()["access_token"]
    admin = {"Authorization": f"Bearer {token}"}
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    return client, admin, node, tmp_path


def user(client, admin, node, rights):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    for right in rights:
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    return {"Authorization": f"Bearer {token}"}


def act(client, headers, node, action, container=ID):
    return client.post(
        f"/api/v1/nodes/{node}/docker/containers/{container}/action",
        headers=headers,
        json={"action": action},
    )


def test_actions_are_audited_and_remove_needs_its_own_right(hub):
    client, admin, node, tmp = hub
    assert act(client, admin, node, "restart").json() == {"done": True}
    bob = user(client, admin, node, ["nodes.read", "docker.read", "docker.control"])
    assert act(client, bob, node, "stop").status_code == 200
    refused = act(client, bob, node, "remove")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "rights.missing"
    assert ("DELETE", f"/containers/{ID}?v=0&force=0&link=0") not in SEEN
    rows = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute(
            "SELECT event, details FROM audit_log"
            " WHERE event = 'docker.container_action' AND outcome = 'success' ORDER BY id"
        )
        .fetchall()
    )
    assert [json.loads(r[1])["action"] for r in rows] == ["restart", "stop"]


def test_unseen_node_is_404_and_read_alone_cannot_act_or_read_logs(hub):
    client, admin, node, _ = hub
    nobody = user(client, admin, node, [])
    assert act(client, nobody, node, "start").status_code == 404
    reader = user_two(client, admin, node)
    assert act(client, reader, node, "start").status_code == 403
    assert (
        client.get(f"/api/v1/nodes/{node}/docker/containers/{ID}/logs", headers=reader).status_code
        == 403
    )
    assert client.get(f"/api/v1/nodes/{node}/docker/images", headers=reader).status_code == 200


def user_two(client, admin, node):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "eve", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "eve", "password": PASSWORD}
    ).json()["access_token"]
    for right in ("nodes.read", "docker.read"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    return {"Authorization": f"Bearer {token}"}


def test_bad_bodies_and_ids_are_refused_before_anything_is_sent(hub):
    client, admin, node, _ = hub
    assert act(client, admin, node, "kill").status_code == 422
    assert act(client, admin, node, "start", container="not-hex").status_code == 422
    assert act(client, admin, node, "start", container="a" * 11).status_code == 422
    assert (
        client.get(f"/api/v1/nodes/{node}/docker/containers/xyz/logs", headers=admin).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/nodes/{node}/docker/containers/{ID}/logs?tail=5000", headers=admin
        ).status_code
        == 422
    )
    assert SEEN == []


def test_refused_attempts_are_audited_but_an_invisible_node_is_not(hub):
    client, admin, node, tmp = hub
    bob = user(client, admin, node, ["nodes.read", "docker.read", "docker.control"])
    assert act(client, bob, node, "remove").status_code == 403
    assert (
        client.get(f"/api/v1/nodes/{node}/docker/containers/{ID}/logs", headers=bob).status_code
        == 403
    )
    nobody = user_two(client, admin, node)
    assert act(client, nobody, "0" * 32, "start").status_code == 404
    rows = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute(
            "SELECT event, outcome, details FROM audit_log WHERE outcome = 'denied' ORDER BY id"
        )
        .fetchall()
    )
    assert [(r[0], json.loads(r[2])["right"]) for r in rows] == [
        ("docker.container_action", "docker.remove"),
        ("docker.logs_read", "docker.logs"),
    ]


def test_missing_container_and_conflict_are_reported_and_audited_as_failures(hub):
    client, admin, node, tmp = hub
    STATUS["POST"] = 404
    assert act(client, admin, node, "start").status_code == 404
    STATUS.clear()
    STATUS["DELETE"] = 409
    conflict = act(client, admin, node, "remove")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "worker.conflict"
    outcomes = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute(
            "SELECT outcome FROM audit_log WHERE event = 'docker.container_action' ORDER BY id"
        )
        .fetchall()
    )
    assert outcomes == [("failure",), ("failure",)]


def test_logs_are_audited_without_their_content_and_lists_work(hub):
    client, admin, node, tmp = hub
    out = client.get(
        f"/api/v1/nodes/{node}/docker/containers/{ID}/logs?tail=50", headers=admin
    ).json()
    assert out["output"] == "out line\nerr line\n"
    row = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute("SELECT details FROM audit_log WHERE event = 'docker.logs_read'")
        .fetchone()[0]
    )
    assert "out line" not in row and ID in row
    for kind in ("images", "networks", "volumes"):
        got = client.get(f"/api/v1/nodes/{node}/docker/{kind}", headers=admin)
        assert got.status_code == 200 and len(got.json()["items"]) == 1


def test_no_api_key_can_carry_the_remove_right(hub):
    client, admin, node, _ = hub
    key = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={
            "name": "ci",
            "rights": ["docker.remove", "docker.control", "docker.read", "nodes.read"],
        },
    )
    assert key.status_code == 422
