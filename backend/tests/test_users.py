import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.registry import load_registry
from armadok.main import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def env(tmp_path, caplog):
    """A running app plus the install code printed in its logs."""
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False), load_registry(("users", "nodes"))
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    return TestClient(app), code


def setup_admin(client, code, username="admin"):
    r = client.post(
        "/api/v1/setup", json={"install_code": code, "username": username, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    return r.json()


def login(client, username="admin", password=PASSWORD):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return r, {"Authorization": f"Bearer {r.json().get('access_token', '')}"}


def test_setup_is_protected_and_one_shot(env):
    client, code = env
    assert client.get("/api/v1/setup/status").json() == {"setup_required": True}
    bad = client.post(
        "/api/v1/setup", json={"install_code": "nope", "username": "admin", "password": PASSWORD}
    )
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "setup.invalid_code"
    weak = client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": "short"}
    )
    assert weak.status_code == 422
    setup_admin(client, code)
    again = client.post(
        "/api/v1/setup", json={"install_code": code, "username": "x1x", "password": PASSWORD}
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "setup.closed"
    assert client.get("/api/v1/setup/status").json() == {"setup_required": False}


def test_login_errors_are_identical_and_cookie_is_hardened(env):
    client, code = env
    setup_admin(client, code)
    wrong_pw, _ = login(client, password="x" * 12)
    unknown, _ = login(client, username="ghost")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()
    ok, _ = login(client)
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api/v1/auth" in cookie


def test_refresh_rotates_and_reuse_revokes_everything(env):
    client, code = env
    setup_admin(client, code)
    _, headers = login(client)
    old_cookie = client.cookies.get("armadok_refresh")
    first = client.post("/api/v1/auth/refresh")
    assert first.status_code == 200
    new_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=new_headers).status_code == 200
    # Replaying the already used refresh token signals theft: all sessions are revoked.
    client.cookies.set("armadok_refresh", old_cookie, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401
    assert client.get("/api/v1/auth/me", headers=new_headers).status_code == 401


def test_logout_ends_the_session(env):
    client, code = env
    setup_admin(client, code)
    _, headers = login(client)
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_token_in_url_is_not_accepted(env):
    client, code = env
    setup_admin(client, code)
    _, headers = login(client)
    token = headers["Authorization"].split()[1]
    assert client.get(f"/api/v1/auth/me?access_token={token}").status_code == 401


def test_admin_manages_users_but_cannot_give_what_it_lacks(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    created = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    )
    assert created.status_code == 201
    bob_id = created.json()["id"]
    assert "password_hash" not in created.json()

    _, bob = login(client, "bob")
    assert client.get("/api/v1/users", headers=bob).status_code == 403

    ok = client.post(
        f"/api/v1/users/{bob_id}/grants", headers=admin, json={"right": "nodes.create"}
    )
    assert ok.status_code == 201
    # The admin holds no global nodes.read, so it cannot hand it out.
    refused = client.post(
        f"/api/v1/users/{bob_id}/grants", headers=admin, json={"right": "nodes.read"}
    )
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "rights.missing"
    invalid = client.post(f"/api/v1/users/{bob_id}/grants", headers=admin, json={"right": "nope"})
    assert invalid.status_code == 422


def test_disabled_account_loses_access_immediately(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    _, bob = login(client, "bob")
    assert client.get("/api/v1/auth/me", headers=bob).status_code == 200
    assert (
        client.patch(
            f"/api/v1/users/{bob_id}", headers=admin, json={"is_active": False}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me", headers=bob).status_code == 401
    assert login(client, "bob")[0].status_code == 401
    admin_id = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    refused = client.patch(f"/api/v1/users/{admin_id}", headers=admin, json={"is_active": False})
    assert refused.status_code == 409


def test_node_creator_owns_it_and_others_do_not_see_it(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    # The administrator has no implicit access: it sees only what it created.
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()
    assert [n["id"] for n in client.get("/api/v1/nodes", headers=admin).json()] == [node["id"]]
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    _, bob = login(client, "bob")
    assert client.get(f"/api/v1/nodes/{node['id']}", headers=bob).status_code == 404
    assert bob_id


def test_limited_admin_cannot_take_over_a_more_privileged_account(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    ids = {}
    for name in ("mallory", "victim"):
        ids[name] = client.post(
            "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
        ).json()["id"]
    # Mallory may reset passwords, but holds far fewer rights than the super-administrator.
    for right in ("users.read", "users.update", "users.reset_password"):
        client.post(f"/api/v1/users/{ids['mallory']}/grants", headers=admin, json={"right": right})
    _, mallory = login(client, "mallory")
    admin_id = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    takeover = client.patch(
        f"/api/v1/users/{admin_id}", headers=mallory, json={"password": "another long password"}
    )
    assert takeover.status_code == 403
    assert login(client, "admin", "another long password")[0].status_code == 401
    # An account with fewer rights than Mallory can be reset.
    ok = client.patch(
        f"/api/v1/users/{ids['victim']}",
        headers=mallory,
        json={"password": "another long password"},
    )
    assert ok.status_code == 200


def test_password_reset_needs_its_own_right(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    ids = [
        client.post(
            "/api/v1/users", headers=admin, json={"username": n, "password": PASSWORD}
        ).json()["id"]
        for n in ("helper", "target")
    ]
    for right in ("users.read", "users.update"):
        client.post(f"/api/v1/users/{ids[0]}/grants", headers=admin, json={"right": right})
    _, helper = login(client, "helper")
    denied = client.patch(f"/api/v1/users/{ids[1]}", headers=helper, json={"password": "x" * 14})
    assert denied.status_code == 403


def test_naive_or_past_expiry_is_rejected(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    url = f"/api/v1/users/{uid}/grants"
    naive = client.post(
        url, headers=admin, json={"right": "nodes.create", "expires_at": "2099-01-01T00:00:00"}
    )
    past = client.post(
        url, headers=admin, json={"right": "nodes.create", "expires_at": "2000-01-01T00:00:00Z"}
    )
    future = client.post(
        url, headers=admin, json={"right": "nodes.create", "expires_at": "2099-01-01T00:00:00Z"}
    )
    assert (naive.status_code, past.status_code, future.status_code) == (422, 422, 201)


def test_login_is_throttled_after_repeated_failures(env):
    client, code = env
    setup_admin(client, code)
    for _ in range(5):
        assert login(client, password="wrong password!")[0].status_code == 401
    blocked, _ = login(client)
    assert (
        blocked.status_code == 429 and blocked.json()["error"]["code"] == "auth.too_many_attempts"
    )


def test_rights_catalogue_is_readable_by_any_account_and_flags_sensitive_rights(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    _, bob = login(client, "bob")
    assert uid
    catalogue = {r["id"]: r for r in client.get("/api/v1/rights", headers=bob).json()}
    assert catalogue["users.reset_password"]["sensitive"] is True
    assert catalogue["nodes.update"]["depends_on"] == ["nodes.read"]
    assert client.get("/api/v1/rights").status_code == 401


def test_me_reports_the_second_factor_state(env):
    client, code = env
    setup_admin(client, code)
    _, admin = login(client)
    me = client.get("/api/v1/auth/me", headers=admin).json()
    assert me["totp_enabled"] is False and me["backup_codes_remaining"] == 0
