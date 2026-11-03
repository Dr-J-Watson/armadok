import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.registry import load_registry
from armadok.main import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def env(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False),
            load_registry(("users", "apikeys", "nodes")),
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


def make_key(client, session, **body):
    payload = {"name": "ci", "rights": ["nodes.create"], **body}
    return client.post("/api/v1/apikeys", headers=session, json=payload)


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_key_is_shown_once_and_stored_hashed(env, tmp_path):
    import sqlite3

    client, session = env
    created = make_key(client, session)
    assert created.status_code == 201
    key = created.json()["key"]
    assert key.startswith("arm_")
    listed = client.get("/api/v1/apikeys", headers=session).json()
    assert len(listed) == 1 and "key" not in listed[0] and "secret_hash" not in listed[0]
    stored = sqlite3.connect(tmp_path / "armadok.db").execute("SELECT secret_hash FROM api_keys")
    assert key.split("_", 2)[2] not in stored.fetchone()[0]


def test_key_works_with_its_rights_only(env):
    client, session = env
    key = make_key(client, session).json()["key"]
    ok = client.post("/api/v1/nodes", headers=bearer(key), json={"name": "n", "type": "docker"})
    assert ok.status_code == 201
    # The owner holds users.read, but the key was not given it.
    assert client.get("/api/v1/users", headers=bearer(key)).status_code == 403
    assert client.get("/api/v1/users", headers=session).status_code == 200


def test_cannot_request_more_than_held_or_sensitive_rights(env):
    client, session = env
    assert make_key(client, session, rights=["nodes.read"]).status_code == 422  # not held globally
    assert (
        make_key(client, session, rights=["users.reset_password"]).status_code == 422
    )  # sensitive
    assert make_key(client, session, rights=["nope"]).status_code == 422
    assert make_key(client, session, rights=["users.update"]).status_code == 422  # needs users.read


def test_read_only_key_cannot_write(env):
    client, session = env
    key = make_key(client, session, rights=["users.read"], read_only=True).json()["key"]
    assert client.get("/api/v1/users", headers=bearer(key)).status_code == 200
    # Safe methods only: the check happens before the route runs.
    refused = client.post("/api/v1/users", headers=bearer(key), json={})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "auth.read_only_key"


def test_key_cannot_manage_keys(env):
    client, session = env
    key = make_key(client, session).json()["key"]
    r = make_key(client, bearer(key))
    assert r.status_code == 403 and r.json()["error"]["code"] == "apikeys.not_with_key"
    assert client.get("/api/v1/apikeys", headers=bearer(key)).status_code == 403


def test_revoked_expired_and_forged_keys_are_refused(env):
    client, session = env
    created = make_key(client, session).json()
    key = created["key"]
    assert client.delete(f"/api/v1/apikeys/{created['id']}", headers=session).status_code == 204
    assert client.get("/api/v1/auth/me", headers=bearer(key)).status_code == 401
    forged = key[:-3] + "xyz"
    assert client.get("/api/v1/auth/me", headers=bearer(forged)).status_code == 401
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert make_key(client, session, expires_at=past).status_code == 422


def test_key_dies_with_its_owner_and_in_real_time_with_rights(env):
    client, session = env
    key = make_key(client, session, rights=["users.read"]).json()["key"]
    assert client.get("/api/v1/users", headers=bearer(key)).status_code == 200
    me = client.get("/api/v1/auth/me", headers=session).json()
    grants = client.get(f"/api/v1/users/{me['id']}/grants", headers=session).json()
    read_id = next(g["id"] for g in grants if g["right"] == "users.read")
    # Remove the owner's right: the key loses it at once (and so does the owner's session).
    client.delete(f"/api/v1/users/{me['id']}/grants/{read_id}", headers=session)
    assert client.get("/api/v1/users", headers=bearer(key)).status_code == 403


def test_actions_by_a_key_are_audited_with_the_key(env, tmp_path):
    import sqlite3

    client, session = env
    created = make_key(client, session).json()
    client.post(
        "/api/v1/nodes", headers=bearer(created["key"]), json={"name": "n", "type": "docker"}
    )
    actors = [
        r[0]
        for r in sqlite3.connect(tmp_path / "armadok.db").execute(
            "SELECT actor FROM audit_log WHERE event = 'node.create'"
        )
    ]
    assert len(actors) == 1 and actors[0].endswith(f"via key:{created['id']}")
