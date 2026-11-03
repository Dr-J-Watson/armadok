import logging
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app

PASSWORD = "correct horse battery"


def build(tmp_path, caplog, key=True):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                encryption_key=generate_key() if key else None,
            ),
            load_registry(("users", "nodes", "audit", "notes")),
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    client = TestClient(app)
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    return client


@pytest.fixture
def env(tmp_path, caplog):
    return build(tmp_path, caplog), tmp_path


def login(client, user="admin"):
    token = client.post("/api/v1/auth/login", json={"username": user, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def note_body(node, **extra):
    return {
        "node_id": node,
        "object_type": "container",
        "object_id": "abc123",
        "body": "renew the cert",
        **extra,
    }


def test_notes_are_private_even_from_the_platform_administrator(env):
    client, _ = env
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    for right in ("nodes.read",):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    bob = login(client, "bob")
    mine = client.post(
        "/api/v1/notes", headers=bob, json=note_body(node, body="bob's secret plan")
    ).json()
    # The administrator (who holds every platform right) neither lists, reads, edits nor deletes it.
    assert client.get("/api/v1/notes", headers=admin).json() == []
    assert (
        client.patch(f"/api/v1/notes/{mine['id']}", headers=admin, json={"body": "x"}).status_code
        == 404
    )
    assert client.delete(f"/api/v1/notes/{mine['id']}", headers=admin).status_code == 404
    assert client.get("/api/v1/notes", headers=bob).json()[0]["body"] == "bob's secret plan"


def test_note_text_is_encrypted_in_the_database_and_never_in_the_audit(env):
    client, tmp_path = env
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    client.post("/api/v1/notes", headers=admin, json=note_body(node, body="my private memo"))
    db = sqlite3.connect(tmp_path / "armadok.db")
    stored = db.execute("SELECT body, encrypted FROM notes").fetchone()
    assert stored[1] == 1 and "private memo" not in stored[0]
    everything = str(db.execute("SELECT * FROM audit_log").fetchall())
    assert "memo" not in everything and "note" not in everything.lower().replace("node", "")


def test_a_note_needs_a_visible_node_and_is_listed_and_filtered(env):
    client, _ = env
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    assert client.post("/api/v1/notes", headers=admin, json=note_body("nope")).status_code == 404
    client.post("/api/v1/notes", headers=admin, json=note_body(node, body="first"))
    client.post(
        "/api/v1/notes",
        headers=admin,
        json={**note_body(node, body="second"), "object_id": "other"},
    )
    by_object = client.get(f"/api/v1/notes?node_id={node}&object_id=abc123", headers=admin).json()
    assert [n["body"] for n in by_object] == ["first"]
    assert [n["body"] for n in client.get("/api/v1/notes?q=SECO", headers=admin).json()] == [
        "second"
    ]
    assert (
        client.post(
            "/api/v1/notes", headers=admin, json={**note_body(node), "object_type": "Bad Type"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/notes", headers=admin, json={**note_body(node), "body": "x" * 4001}
        ).status_code
        == 422
    )


def test_pin_importance_reminder_done_and_concurrent_edit_detection(env):
    client, _ = env
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    created = client.post(
        "/api/v1/notes", headers=admin, json=note_body(node, remind_at="2099-01-01T09:00:00Z")
    ).json()
    url = f"/api/v1/notes/{created['id']}"
    changed = client.patch(
        url,
        headers=admin,
        json={
            "pinned": True,
            "importance": "todo",
            "done": True,
            "expected_updated_at": created["updated_at"],
        },
    ).json()
    assert changed["pinned"] is True and changed["importance"] == "todo" and changed["done"] is True
    stale = client.patch(
        url, headers=admin, json={"body": "mine", "expected_updated_at": created["updated_at"]}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "notes.conflict"
    cleared = client.patch(url, headers=admin, json={"clear_reminder": True}).json()
    assert cleared["remind_at"] is None
    naive = client.patch(url, headers=admin, json={"remind_at": "2099-01-01T09:00:00"})
    assert naive.status_code == 422  # a reminder needs a time zone
    assert client.patch(url, headers=admin, json={"importance": "urgent"}).status_code == 422


def test_pinned_notes_come_first_and_deleting_works(env):
    client, _ = env
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    a = client.post("/api/v1/notes", headers=admin, json=note_body(node, body="a")).json()
    client.post("/api/v1/notes", headers=admin, json=note_body(node, body="b"))
    client.patch(f"/api/v1/notes/{a['id']}", headers=admin, json={"pinned": True})
    assert [n["body"] for n in client.get("/api/v1/notes", headers=admin).json()] == ["a", "b"]
    assert client.delete(f"/api/v1/notes/{a['id']}", headers=admin).status_code == 204
    assert client.delete(f"/api/v1/notes/{a['id']}", headers=admin).status_code == 404
    assert client.get("/api/v1/notes").status_code == 401


def test_without_an_encryption_key_notes_still_work_in_plain_text(tmp_path, caplog):
    client = build(tmp_path, caplog, key=False)
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    client.post("/api/v1/notes", headers=admin, json=note_body(node, body="plain note"))
    assert client.get("/api/v1/notes", headers=admin).json()[0]["body"] == "plain note"
