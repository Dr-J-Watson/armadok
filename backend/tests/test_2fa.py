import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.users import totp

PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        settings = Settings(data_dir=tmp_path, cookie_secure=False, encryption_key=generate_key())
        app = create_app(settings, load_registry(("users", "nodes")))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    c = TestClient(app)
    c.post("/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD})
    return c


def login(client, **extra):
    body = {"username": "admin", "password": PASSWORD, **extra}
    r = client.post("/api/v1/auth/login", json=body)
    return r, {"Authorization": f"Bearer {r.json().get('access_token', '')}"}


def current_code(secret, offset=0):
    import time

    return totp.code_at(secret, int(time.time() // 30) + offset)


def enable_2fa(client):
    _, headers = login(client)
    secret = client.post(
        "/api/v1/auth/2fa/enroll", headers=headers, json={"password": PASSWORD}
    ).json()["secret"]
    r = client.post(
        "/api/v1/auth/2fa/confirm", headers=headers, json={"code": current_code(secret)}
    )
    assert r.status_code == 200, r.text
    return secret, r.json()["backup_codes"], headers


def test_rfc6238_reference_vector():
    # RFC 6238 appendix B (SHA-1), secret "12345678901234567890", T = 59 s -> step 1.
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert totp.code_at(secret, 1) == "287082"


def test_enrol_confirm_then_login_requires_code(client):
    secret, backups, _ = enable_2fa(client)
    assert len(backups) == 8
    missing, _ = login(client)
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "auth.totp_required"
    wrong, _ = login(client, totp_code="000000")
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "auth.invalid_code"
    ok, _ = login(client, totp_code=current_code(secret, 1))
    assert ok.status_code == 200


def test_a_code_cannot_be_replayed(client):
    secret, _, _ = enable_2fa(client)
    # The confirmation code already consumed this time step.
    replay, _ = login(client, totp_code=current_code(secret))
    assert replay.status_code == 401
    assert login(client, totp_code=current_code(secret, 1))[0].status_code == 200


def test_backup_code_works_once(client):
    _, backups, _ = enable_2fa(client)
    assert login(client, totp_code=backups[0])[0].status_code == 200
    assert login(client, totp_code=backups[0])[0].status_code == 401


def test_secret_is_encrypted_in_the_database(client, tmp_path):
    import sqlite3

    secret, _, _ = enable_2fa(client)
    stored = sqlite3.connect(tmp_path / "armadok.db").execute("SELECT secret_enc FROM user_totp")
    assert secret not in stored.fetchone()[0]


def test_disable_needs_password_and_code(client):
    secret, backups, headers = enable_2fa(client)
    url = "/api/v1/auth/2fa/disable"
    bad = client.post(
        url, headers=headers, json={"password": "wrong password!", "code": backups[0]}
    )
    assert bad.status_code == 401
    ok = client.post(url, headers=headers, json={"password": PASSWORD, "code": backups[1]})
    assert ok.status_code == 204
    assert login(client)[0].status_code == 200


def test_enrolment_without_encryption_key_is_a_clear_503(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False), load_registry(("users", "nodes"))
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    c = TestClient(app)
    c.post("/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD})
    _, headers = login(c)
    r = c.post("/api/v1/auth/2fa/enroll", headers=headers, json={"password": PASSWORD})
    assert r.status_code == 503 and r.json()["error"]["code"] == "crypto.key_missing"


def test_enrol_needs_the_password_and_is_throttled(client):
    _, headers = login(client)
    url = "/api/v1/auth/2fa/enroll"
    for _ in range(5):
        assert (
            client.post(url, headers=headers, json={"password": "wrong password!"}).status_code
            == 401
        )
    blocked = client.post(url, headers=headers, json={"password": PASSWORD})
    assert blocked.status_code == 429


def test_enrolment_cannot_overwrite_a_confirmed_secret(client):
    _, _, headers = enable_2fa(client)
    again = client.post("/api/v1/auth/2fa/enroll", headers=headers, json={"password": PASSWORD})
    assert again.status_code == 409
    assert login(client)[0].json()["error"]["code"] == "auth.totp_required"


def test_confirm_ends_other_sessions(client):
    _, old_headers = login(client)
    secret = client.post(
        "/api/v1/auth/2fa/enroll", headers=old_headers, json={"password": PASSWORD}
    ).json()["secret"]
    _, new_headers = login(client)
    client.post(
        "/api/v1/auth/2fa/confirm", headers=new_headers, json={"code": current_code(secret)}
    )
    assert client.get("/api/v1/auth/me", headers=old_headers).status_code == 401
    assert client.get("/api/v1/auth/me", headers=new_headers).status_code == 200


def test_odd_code_input_never_causes_a_500(client):
    secret, _, _ = enable_2fa(client)
    for bad in ("٠٠٠٠٠٠", "123456\n", "x" * 32, "123-456"):
        assert login(client, totp_code=bad)[0].status_code in (401, 429)


def test_admin_can_reset_2fa_but_only_with_the_right(client):
    secret, _, _ = enable_2fa(client)
    ok, admin = login(client, totp_code=current_code(secret, 1))
    uid = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    assert client.delete(f"/api/v1/users/{uid}/2fa", headers=admin).status_code == 204
    assert login(client)[0].status_code == 200
