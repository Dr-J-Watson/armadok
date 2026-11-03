import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.registry import Registry
from armadok.core.rights import Grant, Principal
from armadok.main import create_app
from armadok.modules.nodes.module import module as nodes_module
from armadok.modules.users.module import module as users_module

ADMIN = Principal("admin", (Grant("nodes.read"), Grant("nodes.create")))
READER_ALL = Principal("r", (Grant("nodes.read"),))


def client_for(tmp_path, principal):
    """Build a client whose requests are always made by `principal` (None = anonymous)."""
    app = create_app(
        Settings(data_dir=tmp_path), Registry([users_module, nodes_module]), lambda _r: principal
    )
    return TestClient(app), app


def test_anonymous_gets_401(tmp_path):
    client, _ = client_for(tmp_path, None)
    r = client.get("/api/v1/nodes")
    assert r.status_code == 401 and r.json()["error"]["code"] == "auth.required"


def test_create_needs_the_right_and_is_audited(tmp_path):
    denied, _ = client_for(tmp_path, READER_ALL)
    r = denied.post("/api/v1/nodes", json={"name": "a", "type": "docker"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "rights.missing"

    client, _ = client_for(tmp_path, ADMIN)
    r = client.post("/api/v1/nodes", json={"name": "a", "type": "docker"})
    assert r.status_code == 201 and r.json()["owner"] == "admin"
    conn = sqlite3.connect(tmp_path / "armadok.db")
    assert conn.execute("SELECT event FROM audit_log").fetchall() == [("node.create",)]


def test_invalid_type_is_rejected(tmp_path):
    client, _ = client_for(tmp_path, ADMIN)
    assert client.post("/api/v1/nodes", json={"name": "a", "type": "x"}).status_code == 422


def test_invisible_node_is_404_not_403(tmp_path):
    client, _ = client_for(tmp_path, ADMIN)
    node_id = client.post("/api/v1/nodes", json={"name": "a", "type": "full"}).json()["id"]
    stranger, _ = client_for(tmp_path, Principal("s", (Grant("nodes.read", "node:other"),)))
    assert stranger.get(f"/api/v1/nodes/{node_id}").status_code == 404
    assert stranger.get("/api/v1/nodes").json() == []


def test_update_and_delete_follow_their_own_rights(tmp_path):
    client, _ = client_for(tmp_path, ADMIN)
    node_id = client.post("/api/v1/nodes", json={"name": "a", "type": "full"}).json()["id"]
    scope = f"node:{node_id}"

    reader, _ = client_for(tmp_path, Principal("r", (Grant("nodes.read", scope),)))
    assert reader.patch(f"/api/v1/nodes/{node_id}", json={"name": "b"}).status_code == 403
    assert reader.delete(f"/api/v1/nodes/{node_id}").status_code == 403

    editor, _ = client_for(
        tmp_path,
        Principal(
            "e",
            (
                Grant("nodes.read", scope),
                Grant("nodes.update", scope),
                Grant("nodes.delete", scope),
            ),
        ),
    )
    assert editor.patch(f"/api/v1/nodes/{node_id}", json={"name": "b"}).json()["name"] == "b"
    assert editor.delete(f"/api/v1/nodes/{node_id}").status_code == 204
    assert editor.get(f"/api/v1/nodes/{node_id}").status_code == 404


def test_expired_read_grant_hides_the_node(tmp_path):
    client, _ = client_for(tmp_path, ADMIN)
    node_id = client.post("/api/v1/nodes", json={"name": "a", "type": "full"}).json()["id"]
    past = datetime.now(UTC) - timedelta(minutes=1)
    expired, _ = client_for(tmp_path, Principal("x", (Grant("nodes.read", expires_at=past),)))
    assert expired.get(f"/api/v1/nodes/{node_id}").status_code == 404
