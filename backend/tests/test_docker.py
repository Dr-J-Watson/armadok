import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import httpx
import pytest
import uvicorn

from armadok.core.config import Settings
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.worker import docker_ops
from armadok.worker import main as worker_main
from armadok.worker.ops import OperationError

PASSWORD = "correct horse battery"
MODULES = ("users", "apikeys", "nodes", "workers", "docker", "projects", "relations")

CONTAINERS = [
    {
        "Id": "a" * 64,
        "Names": ["/web"],
        "Image": "nginx:1.27",
        "State": "running",
        "Status": "Up 2 hours",
        "Created": 1700000000,
        "Command": "nginx -g 'daemon off;' --password=hunter2",
        "Labels": {
            "com.docker.compose.project": "demo",
            "traefik.enable": "true",
            "traefik.http.routers.web.rule": "Host(`app.example.com`)",
            "traefik.http.routers.web.middlewares": "auth@docker",
            "traefik.http.middlewares.auth.basicauth.users": "admin:$apr1$abc$hash",
            "traefik.http.middlewares.h.headers.customrequestheaders.Authorization": "Bearer x",
            "com.docker.compose.project.working_dir": "/home/alice/secret-project",
            "secret.token": "must-not-leave",
        },
        "Ports": [{"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"}],
        "NetworkSettings": {"Networks": {"demo_default": {"IPAddress": "172.18.0.2"}}},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "data",
                "Source": "/var/lib/docker/v/data",
                "Destination": "/data",
                "RW": True,
            },
            {"Type": "bind", "Source": "/srv/site", "Destination": "/site", "RW": False},
        ],
    }
]


class FakeDocker(BaseHTTPRequestHandler):
    """A tiny stand-in for the Docker daemon, answering on a Unix socket."""

    def do_GET(self):
        routes = {
            "/version": {"Version": "27.0.1", "ApiVersion": "1.46"},
            "/containers/json?all=1": CONTAINERS,
        }
        body = routes.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body if body is not None else {}).encode())

    def do_POST(self):  # a write must never be attempted by the worker
        self.send_response(500)
        self.end_headers()

    def log_message(self, *args):
        pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    path = str(tmp_path / "docker.sock")
    server = UnixServer(path, FakeDocker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", path)
    yield path
    server.shutdown()


def test_worker_reduces_containers_to_safe_fields(fake_docker):
    out = docker_ops.containers_list({})["containers"][0]
    assert out["names"] == ["web"] and out["networks"] == ["demo_default"]
    assert out["labels"] == {
        "com.docker.compose.project": "demo",
        "traefik.enable": "true",
        "traefik.http.routers.web.rule": "Host(`app.example.com`)",
        "traefik.http.routers.web.middlewares": "auth@docker",
    }
    blob = json.dumps(out)
    assert "apr1" not in blob and "Bearer" not in blob and "alice" not in blob
    assert "Command" not in out and "hunter2" not in json.dumps(out)
    # Host paths are kept for bind mounts only (they link a container to its project folder).
    sources = {m["type"]: m["source"] for m in out["mounts"]}
    assert sources == {"volume": "", "bind": "/srv/site"}
    assert docker_ops.docker_info({}) == {"version": "27.0.1", "api_version": "1.46"}


def test_missing_socket_gives_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", str(tmp_path / "nope.sock"))
    with pytest.raises(OperationError) as caught:
        docker_ops.docker_info({})
    assert caught.value.code == "worker.docker_unavailable"


# --- through the hub, with a real server and worker ---------------------------------------


@pytest.fixture
def live(tmp_path, caplog, fake_docker):
    import logging
    import re

    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False), load_registry(MODULES))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=1 << 20)
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}")
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    yield client, headers, port, tmp_path
    server.should_exit = True


def connect_worker(client, headers, port, tmp_path, node_type):
    node = client.post("/api/v1/nodes", headers=headers, json={"name": "n", "type": node_type})
    node_id = node.json()["id"]
    body = client.post(f"/api/v1/nodes/{node_id}/worker/enrol", headers=headers).json()
    stop = threading.Event()
    threading.Thread(
        target=worker_main.run,
        args=(
            f"ws://127.0.0.1:{port}/api/v1/worker/connect",
            body["hub_key"],
            tmp_path / f"w-{node_type}",
            body["token"],
            stop,
        ),
        daemon=True,
    ).start()
    for _ in range(150):
        if client.get(f"/api/v1/nodes/{node_id}/worker", headers=headers).json()["connected"]:
            break
        time.sleep(0.1)
    return node_id, stop


def test_containers_through_the_hub_and_node_type_gating(live):
    client, headers, port, tmp_path = live
    docker_node, stop1 = connect_worker(client, headers, port, tmp_path, "docker")
    info = client.get(f"/api/v1/nodes/{docker_node}/docker/info", headers=headers)
    assert info.json() == {"version": "27.0.1", "api_version": "1.46"}
    listed = client.get(f"/api/v1/nodes/{docker_node}/docker/containers", headers=headers).json()
    assert listed["containers"][0]["names"] == ["web"]

    # A network-only machine is not asked about Docker.
    net_node, stop2 = connect_worker(client, headers, port, tmp_path, "network")
    refused = client.get(f"/api/v1/nodes/{net_node}/docker/containers", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "worker.op_not_for_node_type"
    stop1.set()
    stop2.set()


def test_docker_needs_the_right_and_a_connected_worker(live):
    client, headers, port, tmp_path = live
    node_id = client.post(
        "/api/v1/nodes", headers=headers, json={"name": "n", "type": "docker"}
    ).json()["id"]
    url = f"/api/v1/nodes/{node_id}/docker/containers"
    assert client.get(url, headers=headers).json()["error"]["code"] == "worker.not_connected"
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob_token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {bob_token}"}
    assert client.get(url, headers=bob).status_code == 404
    client.post(
        f"/api/v1/users/{uid}/grants",
        headers=headers,
        json={"right": "nodes.read", "scope": f"node:{node_id}"},
    )
    assert client.get(url, headers=bob).status_code == 403


def test_huge_host_gets_a_truncated_answer_not_a_broken_connection(fake_docker, monkeypatch):
    heavy = {f"traefik.http.routers.r{n}.rule": "x" * 5000 for n in range(100)}
    many = [
        {"Id": f"{i:064d}", "Names": [f"/c{i}"], "Image": "x" * 5000, "Labels": heavy}
        for i in range(1000)
    ]
    monkeypatch.setattr(docker_ops, "docker_get", lambda path: many)
    out = docker_ops.containers_list({})
    assert out["truncated"] is True and 0 < len(out["containers"]) < 1000
    assert len(json.dumps(out)) < docker_ops.ANSWER_BUDGET_BYTES + 100_000
    assert all(len(c["image"]) <= docker_ops.MAX_TEXT for c in out["containers"])


def test_hub_refuses_loose_or_oversized_worker_data_and_refilters_labels():
    from pydantic import ValidationError

    from armadok.modules.docker import schemas

    ok = {
        "id": "a",
        "labels": {"traefik.enable": "true", "traefik.http.middlewares.a.basicauth.users": "x"},
    }
    parsed = schemas.Container.model_validate(ok)
    assert parsed.labels == {"traefik.enable": "true"}  # the hub filters again
    for bad in (
        {"id": "a", "created": "5"},  # no coercion of "5" to 5
        {"id": "a", "names": ["x" * 301]},
        {"id": "a", "labels": {"k" * 201: "v"}},
        {"id": "a", "networks": ["n"] * 21},
        {"id": "a", "created": -1},
    ):
        with pytest.raises(ValidationError):
            schemas.Container.model_validate(bad)
