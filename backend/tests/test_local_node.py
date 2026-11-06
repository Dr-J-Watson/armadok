import json
import logging
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.registry import load_registry
from armadok.main import create_app

PASSWORD = "correct horse battery"
MODULES = ("users", "nodes", "workers", "docker", "projects", "relations")


class FakeDocker(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {
            "/version": {"Version": "27.0", "ApiVersion": "1.46"},
            "/containers/json?all=1": [],
        }.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body if body is not None else {}).encode())

    def log_message(self, *args):
        pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """The hub's own machine: a Docker socket and a projects folder it may use."""
    sock = str(tmp_path / "docker.sock")
    server = UnixServer(sock, FakeDocker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stacks = tmp_path / "stacks" / "blog"
    stacks.mkdir(parents=True)
    (stacks / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", sock)
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(tmp_path / "stacks"))
    yield tmp_path
    server.shutdown()


def start(tmp_path, caplog, local=True):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path / "data", cookie_secure=False, local_node=local),
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
    return client, {"Authorization": f"Bearer {token}"}


def test_the_local_node_exists_only_when_enabled_and_belongs_to_the_first_account(machine, caplog):
    client, admin = start(machine, caplog)
    nodes = client.get("/api/v1/nodes", headers=admin).json()
    assert [(n["name"], n["type"], n["local"]) for n in nodes] == [("Local", "full", True)]
    off = start(machine / "other", caplog, local=False)
    assert off[0].get("/api/v1/nodes", headers=off[1]).json() == []


def test_docker_and_projects_work_on_the_local_node_without_any_worker(machine, caplog):
    client, admin = start(machine, caplog)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    status = client.get(f"/api/v1/nodes/{node}/worker", headers=admin).json()
    assert status["local"] is True and status["connected"] is True
    assert client.get(f"/api/v1/nodes/{node}/docker/info", headers=admin).json() == {
        "version": "27.0",
        "api_version": "1.46",
    }
    # A folder is only discovered until it is imported.
    base = f"/api/v1/nodes/{node}/projects"
    assert client.get(base, headers=admin).json()["projects"] == []
    assert client.get(f"{base}/discovered", headers=admin).json()["projects"][0]["name"] == "blog"
    client.post(f"{base}/import", headers=admin, json={"projects": ["blog"]})
    assert client.get(base, headers=admin).json()["projects"][0]["name"] == "blog"
    assert client.post(f"/api/v1/nodes/{node}/worker/ping", headers=admin).json() == {"pong": True}
    graph = client.get(f"/api/v1/nodes/{node}/relations", headers=admin).json()
    assert graph["unavailable"] == {}


def test_local_operations_keep_the_same_confinement_and_error_codes(machine, caplog):
    client, admin = start(machine, caplog)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    base = f"/api/v1/nodes/{node}/projects"
    client.post(f"{base}/import", headers=admin, json={"projects": ["blog"]})
    assert (
        client.get(f"{base}/blog/files/compose.yaml", headers=admin).json()["content"]
        == "services: {}\n"
    )
    assert client.get(f"{base}/blog/files/.env", headers=admin).status_code == 422
    stale = client.put(
        f"{base}/blog/files/compose.yaml",
        headers=admin,
        json={"content": "x: 1\n", "expected_sha256": "0" * 64},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "worker.conflict"


def test_the_local_node_has_no_worker_to_enrol_and_cannot_be_deleted(machine, caplog):
    client, admin = start(machine, caplog)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    enrol = client.post(f"/api/v1/nodes/{node}/worker/enrol", headers=admin)
    assert enrol.status_code == 409 and enrol.json()["error"]["code"] == "workers.local_node"
    assert client.delete(f"/api/v1/nodes/{node}/worker", headers=admin).status_code == 409
    gone = client.delete(f"/api/v1/nodes/{node}", headers=admin)
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "nodes.local_not_deletable"


def test_other_accounts_do_not_see_the_local_node_and_a_restart_changes_nothing(machine, caplog):
    client, admin = start(machine, caplog)
    client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/nodes", headers=bob).json() == []
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    assert client.get(f"/api/v1/nodes/{node}/docker/info", headers=bob).status_code == 404
    # Starting again on the same data neither duplicates the node nor the owner's rights.
    again = create_app(
        Settings(data_dir=machine / "data", cookie_secure=False, local_node=True),
        load_registry(MODULES),
    )
    nodes = TestClient(again).get("/api/v1/nodes", headers=admin).json()
    assert len(nodes) == 1 and nodes[0]["id"] == node


def test_an_unconfigured_local_machine_gives_a_clear_error_not_a_crash(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.delenv("ARMADOK_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", str(tmp_path / "nope.sock"))
    client, admin = start(tmp_path, caplog)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    projects = client.get(f"/api/v1/nodes/{node}/projects", headers=admin)
    assert (
        projects.status_code == 409
        and projects.json()["error"]["code"] == "worker.projects_not_configured"
    )
    docker = client.get(f"/api/v1/nodes/{node}/docker/info", headers=admin)
    assert docker.json()["error"]["code"] == "worker.docker_unavailable"
