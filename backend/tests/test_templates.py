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
