import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from tests.test_members_admin import PASSWORD, login, person

COMPOSE = "services:\n  web:\n    image: nginx:1.27\n"


@pytest.fixture
def box(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False, encryption_key=generate_key())
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app


def test_templates_are_private_encrypted_and_audited_without_the_text(box):
    client, admin, app = box
    made = client.post(
        "/api/v1/templates",
        headers=admin,
        json={"name": "web", "description": "nginx", "compose": COMPOSE},
    )
    assert made.status_code == 201
    tid = made.json()["id"]
    assert client.get("/api/v1/templates", headers=admin).json()[0]["name"] == "web"
    assert "compose" not in client.get("/api/v1/templates", headers=admin).json()[0]
    assert client.get(f"/api/v1/templates/{tid}", headers=admin).json()["compose"] == COMPOSE
    conn = app.state.settings.open_db()
    stored = conn.execute("SELECT compose, encrypted FROM project_templates").fetchone()
    conn.close()
    assert stored["encrypted"] == 1 and "nginx" not in stored["compose"]
    assert "nginx" not in client.get("/api/v1/audit", headers=admin).text
    # Someone else neither sees nor deletes it.
    _, pat = person(client, admin, "pat")
    assert client.get("/api/v1/templates", headers=pat).json() == []
    assert client.get(f"/api/v1/templates/{tid}", headers=pat).status_code == 404
    assert client.delete(f"/api/v1/templates/{tid}", headers=pat).status_code == 404
    assert client.delete(f"/api/v1/templates/{tid}", headers=admin).status_code == 204


def test_templates_refuse_api_keys_and_oversized_texts(box):
    client, admin, _ = box
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["templates.use"]}
    )
    if key.status_code < 300:
        token = {"Authorization": f"Bearer {key.json()['key']}"}
        assert client.get("/api/v1/templates", headers=token).status_code in (401, 403)
    big = {"name": "x", "compose": "a" * (128 * 1024 + 1)}
    assert client.post("/api/v1/templates", headers=admin, json=big).status_code == 422
    assert (
        client.post(
            "/api/v1/templates", headers=admin, json={"name": "", "compose": "a"}
        ).status_code
        == 422
    )


def test_a_template_can_be_shared_with_everyone_or_with_your_groups_but_only_you_change_it(box):
    client, admin, app = box
    tid = client.post(
        "/api/v1/templates", headers=admin, json={"name": "web", "compose": COMPOSE}
    ).json()["id"]
    pat_id, pat = person(client, admin, "pat")
    _, lou = person(client, admin, "lou")
    conn = app.state.settings.open_db()
    admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
    conn.executemany(
        "INSERT INTO groups (id, name, created_at) VALUES (?, ?, 'now')",
        [("g1", "team"), ("g2", "other")],
    )
    conn.executemany(
        "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
        [("g1", admin_id), ("g1", pat_id), ("g2", pat_id)],
    )
    conn.commit()
    conn.close()
    url = f"/api/v1/templates/{tid}"
    assert client.get(url, headers=pat).status_code == 404  # private until shared
    # You may only share with groups you belong to.
    refused = client.put(url + "/sharing", headers=admin, json={"groups": ["g2"]})
    assert refused.status_code == 422
    assert client.put(url + "/sharing", headers=admin, json={"groups": ["g1"]}).status_code == 204
    assert client.get(url, headers=pat).json()["compose"] == COMPOSE
    listed = client.get("/api/v1/templates", headers=pat).json()
    assert listed[0]["mine"] is False and listed[0]["owner_name"] == "admin"
    assert client.get(url, headers=lou).status_code == 404  # not in the group
    # Using is not owning: no sharing change, no deletion.
    assert client.put(url + "/sharing", headers=pat, json={"everyone": True}).status_code == 404
    assert client.delete(url, headers=pat).status_code == 404
    assert client.put(url + "/sharing", headers=admin, json={"everyone": True}).status_code == 204
    assert client.get(url, headers=lou).status_code == 200
    assert client.put(url + "/sharing", headers=admin, json={}).status_code == 204
    assert client.get(url, headers=lou).status_code == 404
    names = [g["name"] for g in client.get("/api/v1/templates/groups", headers=pat).json()]
    assert names == ["other", "team"]
    assert "share" in client.get("/api/v1/audit", headers=admin).text
