import base64
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.apikeys import oauth

PASSWORD = "correct horse battery"


@pytest.fixture
def box(tmp_path, caplog):
    oauth.limiter.clear()
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
            )
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        admin = login(client, "admin")
        node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
        yield client, admin, node, tmp_path


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def person(client, admin, name, grants=()):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right, scope in grants:
        r = client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
        assert r.status_code == 201, r.text
    return uid, login(client, name)


def key_header(full):
    return {"Authorization": f"Bearer {full}"}


def make_account(client, headers, name="ci", **body):
    return client.post("/api/v1/service-accounts", headers=headers, json={"name": name, **body})


def make_key(client, headers, account, rights=("nodes.read",), **extra):
    return client.post(
        f"/api/v1/service-accounts/{account}/keys",
        headers=headers,
        json={"name": "k", "rights": list(rights), **extra},
    )


# --- service accounts -----------------------------------------------------------------------


def test_a_service_account_cannot_log_in_and_shows_as_one_in_the_user_list(box):
    client, admin, node, _ = box
    made = make_account(client, admin)
    assert made.status_code == 201 and made.json()["username"] == "svc-ci"
    for guess in (PASSWORD, "", "svc-ci", "password1234567"):
        bad = client.post("/api/v1/auth/login", json={"username": "svc-ci", "password": guess})
        assert bad.status_code == 401 and bad.json()["error"]["code"] == "auth.invalid_credentials"
    users = {u["username"]: u for u in client.get("/api/v1/users", headers=admin).json()}
    assert users["svc-ci"]["kind"] == "service" and users["admin"]["kind"] == "person"


def test_the_keys_of_a_service_account_follow_the_person_who_made_it(box):
    client, admin, node, _ = box
    bob_id, bob = person(
        client,
        admin,
        "bob",
        [
            ("service_accounts.manage", "*"),
            ("nodes.read", f"node:{node}"),
            ("projects.read", f"node:{node}"),
        ],
    )
    account = make_account(client, bob).json()["id"]
    key = make_key(client, bob, account, ["nodes.read"])
    assert key.status_code == 201, key.text
    headers = key_header(key.json()["key"])
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200
    # Bob loses the right: the account loses it with him.
    grants = client.get(f"/api/v1/users/{bob_id}/grants", headers=admin).json()
    gone = next(g["id"] for g in grants if g["right"] == "nodes.read")
    client.delete(f"/api/v1/users/{bob_id}/grants/{gone}", headers=admin)
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 404


def test_when_the_responsible_person_is_disabled_a_following_account_stops(box):
    client, admin, node, _ = box
    bob_id, bob = person(
        client, admin, "bob", [("service_accounts.manage", "*"), ("nodes.read", f"node:{node}")]
    )
    account = make_account(client, bob).json()["id"]
    headers = key_header(make_key(client, bob, account).json()["key"])
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200
    client.patch(f"/api/v1/users/{bob_id}", headers=admin, json={"is_active": False})
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 401


def test_a_fixed_rights_account_keeps_exactly_what_it_was_given(box):
    client, admin, node, _ = box
    bob_id, bob = person(
        client,
        admin,
        "bob",
        [
            ("service_accounts.manage", "*"),
            ("nodes.read", f"node:{node}"),
            ("docker.read", f"node:{node}"),
        ],
    )
    account = make_account(
        client,
        bob,
        "fixed",
        follow_owner=False,
        grants=[{"right": "nodes.read", "scope": f"node:{node}"}],
    ).json()["id"]
    headers = key_header(make_key(client, bob, account, ["nodes.read"]).json()["key"])
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200
    # It does not hold docker.read, even though its maker does.
    assert (
        make_key(client, bob, account, ["docker.read"]).json()["error"]["code"]
        == "apikeys.invalid_right"
    )
    grants = client.get(f"/api/v1/users/{bob_id}/grants", headers=admin).json()
    for g in grants:
        if g["right"] == "nodes.read":
            client.delete(f"/api/v1/users/{bob_id}/grants/{g['id']}", headers=admin)
    assert (
        client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200
    )  # fixed, as asked


def test_nobody_gives_a_service_account_more_than_they_hold(box):
    client, admin, node, _ = box
    _, bob = person(
        client, admin, "bob", [("service_accounts.manage", "*"), ("nodes.read", f"node:{node}")]
    )
    too_much = make_account(
        client,
        bob,
        "big",
        follow_owner=False,
        grants=[{"right": "docker.read", "scope": f"node:{node}"}],
    )
    assert too_much.status_code == 403
    assert make_account(client, bob, "none", follow_owner=False, grants=[]).status_code == 422
    assert (
        make_account(
            client, bob, "both", follow_owner=True, grants=[{"right": "nodes.read", "scope": "*"}]
        ).status_code
        == 422
    )
    account = make_account(client, bob).json()["id"]
    assert (
        make_key(client, bob, account, ["docker.read"]).json()["error"]["code"]
        == "apikeys.invalid_right"
    )
    assert (
        make_key(client, bob, account, ["service_accounts.manage"]).json()["error"]["code"]
        == "apikeys.invalid_right"
    )
    assert (
        make_key(
            client, bob, account, ["nodes.read"], expires_at="2000-01-01T00:00:00+00:00"
        ).status_code
        == 422
    )


def test_only_the_responsible_person_manages_an_account_and_a_key_never_does(box):
    client, admin, node, _ = box
    _, bob = person(client, admin, "bob", [("service_accounts.manage", "*")])
    _, carol = person(client, admin, "carol", [("service_accounts.manage", "*")])
    account = make_account(client, bob).json()["id"]
    assert client.get("/api/v1/service-accounts", headers=carol).json() == []
    assert client.get(f"/api/v1/service-accounts/{account}/keys", headers=carol).status_code == 404
    assert make_key(client, carol, account).status_code == 404
    assert client.delete(f"/api/v1/service-accounts/{account}", headers=carol).status_code == 404
    _, nobody = person(client, admin, "eve")
    assert client.get("/api/v1/service-accounts", headers=nobody).status_code == 403
    k = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "x", "rights": ["service_accounts.manage"]}
    )
    assert k.status_code == 422  # sensitive: never on a key


def test_closing_an_account_revokes_its_keys_and_disables_it(box):
    client, admin, node, tmp = box
    _, bob = person(
        client, admin, "bob", [("service_accounts.manage", "*"), ("nodes.read", f"node:{node}")]
    )
    account = make_account(client, bob).json()["id"]
    full = make_key(client, bob, account).json()["key"]
    assert client.delete(f"/api/v1/service-accounts/{account}", headers=bob).status_code == 204
    assert client.get(f"/api/v1/nodes/{node}", headers=key_header(full)).status_code == 401
    users = {u["username"]: u for u in client.get("/api/v1/users", headers=admin).json()}
    assert users["svc-ci"]["is_active"] is False


def test_names_are_unique_and_checked_and_everything_is_audited(box):
    client, admin, node, tmp = box
    assert make_account(client, admin).status_code == 201
    assert make_account(client, admin).status_code == 409
    for bad in ("Has Space", "../x", "-x", "a" * 40, ""):
        assert make_account(client, admin, bad).status_code == 422
    account = client.get("/api/v1/service-accounts", headers=admin).json()[0]["id"]
    made = make_key(client, admin, account, ["nodes.read"]).json()
    client.delete(f"/api/v1/service-accounts/{account}/keys/{made['id']}", headers=admin)
    events = [
        r[0]
        for r in sqlite3.connect(tmp / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'service_account.%' ORDER BY id"
        )
    ]
    assert events == [
        "service_account.create",
        "service_account.key_create",
        "service_account.key_revoke",
    ]
    row = (
        sqlite3.connect(tmp / "armadok.db")
        .execute("SELECT actor FROM audit_log WHERE event = 'service_account.key_create'")
        .fetchone()
    )
    assert "key:" not in row[0]  # done by a person's session


# --- OAuth2 client credentials -------------------------------------------------------------


def token_for(client, full, *, basic=False, **form):
    key_id, secret = full[4:].split("_", 1)
    if basic:
        header = {
            "Authorization": "Basic " + base64.b64encode(f"{key_id}:{secret}".encode()).decode()
        }
        return client.post(
            "/api/v1/oauth/token", data={"grant_type": "client_credentials", **form}, headers=header
        )
    return client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": key_id,
            "client_secret": secret,
            **form,
        },
    )


def a_key(client, admin, **extra):
    return client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "t", "rights": ["nodes.read"], **extra}
    ).json()


def test_a_token_acts_like_its_key_and_is_short_lived(box):
    client, admin, node, tmp = box
    key = a_key(client, admin)
    for basic in (False, True):
        answer = token_for(client, key["key"], basic=basic)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert (
            body["token_type"] == "Bearer"
            and body["expires_in"] == 3600
            and body["access_token"].startswith("armt_")
        )
        assert answer.headers["cache-control"] == "no-store"
        headers = key_header(body["access_token"])
        assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200
    db = sqlite3.connect(tmp / "armadok.db")
    stored = "".join(str(c) for row in db.execute("SELECT * FROM oauth_tokens") for c in row)
    assert body["access_token"] not in stored  # only a hash is kept
    db.execute(
        "UPDATE oauth_tokens SET expires_at = ?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    db.commit()
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 401


def test_a_token_has_only_the_rights_of_its_key_and_its_limits(box):
    client, admin, node, _ = box
    key = a_key(client, admin, read_only=True)
    headers = key_header(token_for(client, key["key"]).json()["access_token"])
    assert client.get(f"/api/v1/nodes/{node}/docker/info", headers=headers).status_code in (
        403,
        404,
    )  # no docker right
    refused = client.post("/api/v1/groups", headers=headers, json={"name": "x"})
    assert refused.status_code == 403
    # A token cannot make keys or service accounts either.
    assert (
        client.post(
            "/api/v1/apikeys", headers=headers, json={"name": "x", "rights": ["nodes.read"]}
        ).status_code
        == 403
    )


def test_revoking_the_key_ends_its_tokens(box):
    client, admin, node, _ = box
    key = a_key(client, admin)
    headers = key_header(token_for(client, key["key"]).json()["access_token"])
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200
    client.delete(f"/api/v1/apikeys/{key['id']}", headers=admin)
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 401
    assert token_for(client, key["key"]).status_code == 401


def test_wrong_credentials_and_odd_requests_get_standard_errors(box):
    client, admin, node, _ = box
    key = a_key(client, admin)
    bad = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": key["id"] + "0"[:0],
            "client_secret": "nope",
        },
    )
    assert bad.status_code == 401 and bad.json() == {"error": "invalid_client"}
    assert "Basic" in bad.headers["www-authenticate"]
    assert client.post(
        "/api/v1/oauth/token", data={"grant_type": "password", "username": "a", "password": "b"}
    ).json() == {"error": "unsupported_grant_type"}
    assert client.post("/api/v1/oauth/token", data={"grant_type": "client_credentials"}).json() == {
        "error": "invalid_client"
    }
    big = client.post(
        "/api/v1/oauth/token",
        content=b"grant_type=client_credentials&x=" + b"a" * 9000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert big.status_code == 400
    wrong_secret = token_for(client, key["key"][:-3] + "abc")
    assert wrong_secret.status_code == 401


def test_guessing_is_slowed_down_but_the_right_secret_always_works(box):
    client, admin, node, _ = box
    key = a_key(client, admin)
    for _ in range(20):
        assert (
            client.post(
                "/api/v1/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": key["id"],
                    "client_secret": "x" * 30,
                },
            ).status_code
            == 401
        )
    slowed = client.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": key["id"],
            "client_secret": "x" * 30,
        },
    )
    assert slowed.status_code == 429
    assert token_for(client, key["key"]).status_code == 200


def test_each_key_keeps_a_bounded_number_of_tokens(box, monkeypatch):
    client, admin, node, tmp = box
    monkeypatch.setattr(oauth, "MAX_LIVE_TOKENS_PER_KEY", 3)
    key = a_key(client, admin)
    for _ in range(6):
        assert token_for(client, key["key"]).status_code == 200
    count = (
        sqlite3.connect(tmp / "armadok.db")
        .execute("SELECT COUNT(*) FROM oauth_tokens")
        .fetchone()[0]
    )
    assert count == 3


def test_a_service_account_key_works_with_oauth_too(box):
    client, admin, node, _ = box
    account = make_account(client, admin).json()["id"]
    full = make_key(client, admin, account, ["nodes.read"]).json()["key"]
    headers = key_header(token_for(client, full).json()["access_token"])
    assert client.get(f"/api/v1/nodes/{node}", headers=headers).status_code == 200


# --- findings of the security review ---------------------------------------------------------


def test_a_fixed_rights_account_stops_when_its_responsible_person_is_disabled(box):
    client, admin, node, _ = box
    bob_id, bob = person(
        client, admin, "bob", [("service_accounts.manage", "*"), ("nodes.read", f"node:{node}")]
    )
    account = make_account(
        client,
        bob,
        "fixed",
        follow_owner=False,
        grants=[{"right": "nodes.read", "scope": f"node:{node}"}],
    ).json()["id"]
    full = make_key(client, bob, account).json()["key"]
    assert client.get(f"/api/v1/nodes/{node}", headers=key_header(full)).status_code == 200
    client.patch(f"/api/v1/users/{bob_id}", headers=admin, json={"is_active": False})
    assert client.get(f"/api/v1/nodes/{node}", headers=key_header(full)).status_code == 401
    # Someone else who may manage service accounts can now see and close it.
    _, carol = person(client, admin, "carol", [("service_accounts.manage", "*")])
    assert client.get(f"/api/v1/service-accounts/{account}/keys", headers=carol).status_code == 200
    assert client.delete(f"/api/v1/service-accounts/{account}", headers=carol).status_code == 204
    # ... but not while the person is still around.
    other = make_account(client, admin, "other").json()["id"]
    assert client.get(f"/api/v1/service-accounts/{other}/keys", headers=carol).status_code == 404


def test_a_service_account_has_no_password_or_second_factor_to_reset(box):
    client, admin, node, _ = box
    account = make_account(client, admin).json()["id"]
    pw = client.patch(
        f"/api/v1/users/{account}", headers=admin, json={"password": "another long passphrase"}
    )
    assert pw.status_code == 409 and pw.json()["error"]["code"] == "users.not_a_person"
    assert client.delete(f"/api/v1/users/{account}/2fa", headers=admin).status_code == 409


def test_a_person_cannot_take_a_service_account_name_and_a_closed_account_gets_no_keys(box):
    client, admin, node, _ = box
    taken = client.post(
        "/api/v1/users", headers=admin, json={"username": "svc-ci", "password": PASSWORD}
    )
    assert taken.status_code == 409
    assert (
        client.post(
            "/api/v1/users", headers=admin, json={"username": "SVC-Other", "password": PASSWORD}
        ).status_code
        == 409
    )
    account = make_account(client, admin).json()["id"]
    client.delete(f"/api/v1/service-accounts/{account}", headers=admin)
    assert make_key(client, admin, account).json()["error"]["code"] == "service_accounts.closed"


def test_a_following_account_is_not_a_way_round_the_outranks_rule(box):
    client, admin, node, _ = box
    bob_id, bob = person(
        client,
        admin,
        "bob",
        [
            ("service_accounts.manage", "*"),
            ("nodes.read", f"node:{node}"),
            ("docker.read", f"node:{node}"),
        ],
    )
    account = make_account(client, bob).json()["id"]
    carol_id, carol = person(
        client,
        admin,
        "carol",
        [("users.read", "*"), ("users.update", "*"), ("nodes.read", f"node:{node}")],
    )
    # The account follows Bob and so holds docker.read; Carol does not, so she cannot disable it.
    r = client.patch(f"/api/v1/users/{account}", headers=carol, json={"is_active": False})
    assert r.status_code == 403


def test_the_audit_says_which_rights_an_account_and_its_keys_got(box):
    client, admin, node, tmp = box
    account = make_account(
        client,
        admin,
        "fixed",
        follow_owner=False,
        grants=[{"right": "nodes.read", "scope": f"node:{node}"}],
    ).json()["id"]
    make_key(client, admin, account, ["nodes.read"], read_only=True)
    db = sqlite3.connect(tmp / "armadok.db")
    created = db.execute(
        "SELECT details FROM audit_log WHERE event = 'service_account.create'"
    ).fetchone()[0]
    keyed = db.execute(
        "SELECT details FROM audit_log WHERE event = 'service_account.key_create'"
    ).fetchone()[0]
    assert f"nodes.read@node:{node}" in created
    assert '"read_only": true' in keyed and "nodes.read" in keyed


def test_oauth_refusals_and_tokens_are_audited_without_flooding_the_log(box):
    client, admin, node, tmp = box
    key = a_key(client, admin)
    for _ in range(5):
        client.post(
            "/api/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": key["id"],
                "client_secret": "x" * 30,
            },
        )
        client.post(
            "/api/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "f" * 12,
                "client_secret": "x" * 30,
            },
        )
        token_for(client, key["key"])
    db = sqlite3.connect(tmp / "armadok.db")
    rows = db.execute(
        "SELECT event, outcome, object_id FROM audit_log WHERE event LIKE 'oauth.%' ORDER BY id"
    ).fetchall()
    assert sorted(r[0] for r in rows) == ["oauth.refused", "oauth.token"]
    assert {r[2] for r in rows} == {key["id"]}  # nothing for a key that does not exist


def test_the_oauth_endpoint_stops_reading_a_huge_body(box):
    client, admin, node, _ = box
    chunks = (b"a" * 1024 for _ in range(100))
    r = client.post(
        "/api/v1/oauth/token",
        content=chunks,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400 and r.json() == {"error": "invalid_request"}


def test_flooding_one_kind_of_key_does_not_erase_the_others(box):
    from armadok.core.throttle import FailureLimiter

    limiter = FailureLimiter({"ip": 3, "client": 3}, max_tracked=20)
    for _ in range(3):
        limiter.remember(ip="203.0.113.9")
    for i in range(500):
        limiter.remember(client=f"c{i}")
    assert limiter.blocked(ip="203.0.113.9")
