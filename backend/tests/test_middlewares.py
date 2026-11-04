import hashlib
import logging
import re

import bcrypt
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.traefik import middlewares as mw
from armadok.worker import traefik_ops
from armadok.worker.operation import OperationError

PASSWORD = "correct horse battery"
MODULES = ("users", "apikeys", "nodes", "workers", "traefik", "audit", "relations")
ADAPTER = TypeAdapter(mw.Spec)


def make(**spec):
    return ADAPTER.validate_python(spec)


def doc(spec):
    return yaml.safe_load(mw.render(mw.resolve(spec)))["http"]["middlewares"][spec.name]


# --- the generated text ---------------------------------------------------------------------


def test_each_intention_becomes_a_traefik_middleware():
    assert doc(make(intent="local_only", name="lan"))["ipAllowList"]["sourceRange"][:2] == [
        "127.0.0.1/32",
        "::1/128",
    ]
    assert doc(make(intent="local_only", name="lan", ranges=["203.0.113.0/24", "198.51.100.7"]))[
        "ipAllowList"
    ]["sourceRange"] == ["203.0.113.0/24", "198.51.100.7/32"]
    assert doc(make(intent="redirect_https", name="https")) == {
        "redirectScheme": {"scheme": "https", "permanent": True}
    }
    assert doc(make(intent="strip_prefix", name="strip", prefixes=["/app"])) == {
        "stripPrefix": {"prefixes": ["/app"]}
    }
    assert doc(make(intent="rate_limit", name="slow", average=10, burst=5, period_seconds=2)) == {
        "rateLimit": {"average": 10, "burst": 5, "period": "2s"}
    }
    assert "compress" in doc(make(intent="compress", name="zip"))
    forward = doc(
        make(
            intent="forward_auth",
            name="sso",
            address="https://auth.example.com/verify",
            response_headers=["X-User"],
        )
    )["forwardAuth"]
    assert forward["address"] == "https://auth.example.com/verify"
    assert forward["authResponseHeaders"] == ["X-User"] and forward["trustForwardHeader"] is False


def test_security_headers_have_safe_defaults_and_options():
    headers = doc(make(intent="security_headers", name="safe"))["headers"]
    assert headers["frameDeny"] is True and headers["contentTypeNosniff"] is True
    assert headers["stsSeconds"] == 31536000 and headers["referrerPolicy"].startswith(
        "strict-origin"
    )
    off = doc(
        make(intent="security_headers", name="safe", hsts_seconds=0, csp="default-src 'self'")
    )["headers"]
    assert "stsSeconds" not in off and off["contentSecurityPolicy"] == "default-src 'self'"


@pytest.mark.parametrize(
    "bad",
    [
        {"intent": "local_only", "name": "x", "ranges": ["not an address"]},
        {"intent": "local_only", "name": "x", "ranges": []},
        {"intent": "strip_prefix", "name": "x", "prefixes": ["/a`)"]},
        {"intent": "strip_prefix", "name": "x", "prefixes": ["/"]},
        {"intent": "forward_auth", "name": "x", "address": "ftp://auth"},
        {"intent": "forward_auth", "name": "x", "address": "https://u:p" + "@" + "example.com"},
        {"intent": "forward_auth", "name": "x", "address": "https://auth.example.com/?a=1"},
        {
            "intent": "forward_auth",
            "name": "x",
            "address": "https://auth.example.com",
            "response_headers": ["Bad Header"],
        },
        {"intent": "security_headers", "name": "x", "csp": "default-src 'self'\nX-Evil: 1"},
        {"intent": "security_headers", "name": "x", "csp": "a`b"},
        {"intent": "security_headers", "name": "x", "referrer_policy": "unsafe-url"},
        {"intent": "rate_limit", "name": "x", "average": 0},
        {"intent": "nothing", "name": "x"},
        {"intent": "compress", "name": "Bad Name"},
        {"intent": "compress", "name": "../x"},
        {"intent": "compress", "name": "x", "unknown": 1},
        {"intent": "basic_auth", "name": "x", "users": []},
        {
            "intent": "basic_auth",
            "name": "x",
            "users": [{"username": "a b", "password": "longenough"}],
        },
        {"intent": "basic_auth", "name": "x", "users": [{"username": "a", "password": "short"}]},
        {
            "intent": "basic_auth",
            "name": "x",
            "users": [
                {"username": "a", "password": "longenough"},
                {"username": "a", "password": "longenough"},
            ],
        },
    ],
)
def test_nothing_unchecked_can_reach_the_file(bad):
    with pytest.raises(ValidationError):
        make(**bad)


def test_a_password_becomes_a_bcrypt_hash_and_never_appears_in_clear():
    spec = make(
        intent="basic_auth",
        name="private",
        users=[{"username": "alice", "password": "s3cret-pass-phrase"}],
    )
    resolved = mw.resolve(spec)
    text = mw.render(resolved)
    assert "s3cret-pass-phrase" not in text and "password" not in text.split("\n", 2)[1]
    entry = yaml.safe_load(text)["http"]["middlewares"]["private"]["basicAuth"]["users"][0]
    username, hashed = entry.split(":", 1)
    assert username == "alice" and hashed.startswith("$2y$12$")
    assert bcrypt.checkpw(b"s3cret-pass-phrase", hashed.replace("$2y$", "$2b$", 1).encode())
    assert "s3cret" not in repr(spec) and "password" not in resolved.model_dump_json()


def test_a_user_keeps_the_old_hash_unless_a_new_password_is_given():
    first = mw.resolve(
        make(
            intent="basic_auth",
            name="p",
            users=[{"username": "alice", "password": "first-password"}],
        )
    )
    again = mw.resolve(
        make(
            intent="basic_auth",
            name="p",
            users=[{"username": "alice"}, {"username": "bob", "password": "second-password"}],
        ),
        first,
    )
    users = {u.username: u.hash for u in again.users}
    assert users["alice"] == first.users[0].hash and users["bob"].startswith("$2y$")
    with pytest.raises(ValueError):
        mw.resolve(make(intent="basic_auth", name="p", users=[{"username": "carol"}]), first)


def test_a_password_longer_than_bcrypt_reads_is_refused_not_cut():
    with pytest.raises(ValueError):
        mw.hash_password("x" * 73)


def test_the_form_edits_only_what_it_wrote():
    spec = make(intent="rate_limit", name="slow")
    text = mw.render(spec)
    assert mw.untouched(text) == spec
    assert mw.untouched(text + "# my own note\n") is None
    assert mw.spec_of("http: {}\n") is None
    assert mw.spec_of(mw.SPEC_PREFIX + "{not json}\n") is None
    assert mw.spec_of(mw.SPEC_PREFIX + "x" * (mw.MAX_SPEC_LINE + 1)) is None


# --- the worker, on a real folder -----------------------------------------------------------


@pytest.fixture
def folder(tmp_path, monkeypatch):
    root = tmp_path / "dyn"
    root.mkdir()
    monkeypatch.setenv("ARMADOK_TRAEFIK_DIR", str(root))
    return root


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_only_a_generated_middleware_file_can_be_deleted(folder):
    traefik_ops.middleware_write({"name": "lan", "content": "a: 1\n", "expected_sha256": None})
    (folder / "other").mkdir()
    traefik_ops.file_write({"path": "other/keep.yml", "content": "b: 2\n", "expected_sha256": None})
    for path in (
        "other/keep.yml",
        "middlewares/../other/keep.yml",
        "middlewares/Bad.yml",
        "keep.yml",
        "middlewares/a/b.yml",
    ):
        with pytest.raises(OperationError):
            traefik_ops.middleware_delete({"path": path, "expected_sha256": sha("b: 2\n")})
    assert (folder / "other" / "keep.yml").exists()
    with pytest.raises(OperationError) as stale:
        traefik_ops.middleware_delete({"path": "middlewares/lan.yml", "expected_sha256": "0" * 64})
    assert stale.value.code == "worker.conflict"
    assert traefik_ops.middleware_delete(
        {"path": "middlewares/lan.yml", "expected_sha256": sha("a: 1\n")}
    ) == {"done": True}
    assert not (folder / "middlewares" / "lan.yml").exists()


# --- through the hub ------------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path, folder, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
            ),
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
    admin = {"Authorization": f"Bearer {token}"}
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    return client, admin, node, folder


def url(node, tail=""):
    return f"/api/v1/nodes/{node}/traefik/middlewares{tail}"


LAN = {"spec": {"intent": "local_only", "name": "lan"}}
AUTH = {
    "spec": {
        "intent": "basic_auth",
        "name": "private",
        "users": [{"username": "alice", "password": "s3cret-pass-phrase"}],
    }
}


def test_create_list_change_and_delete(hub):
    client, admin, node, folder = hub
    made = client.post(url(node), headers=admin, json=LAN)
    assert made.status_code == 201 and made.json()["reference"] == "lan@file"
    assert mw.untouched((folder / "middlewares" / "lan.yml").read_text()) is not None
    listed = client.get(url(node), headers=admin).json()["middlewares"]
    assert [(m["name"], m["editable"], m["used_by"]) for m in listed] == [("lan", True, [])]
    sha_now = listed[0]["sha256"]
    changed = client.put(
        url(node, "/lan"),
        headers=admin,
        json={
            "spec": {"intent": "local_only", "name": "lan", "ranges": ["10.0.0.0/8"]},
            "expected_sha256": sha_now,
        },
    )
    assert changed.status_code == 200
    assert "10.0.0.0/8" in (folder / "middlewares" / "lan.yml").read_text()
    stale = client.put(
        url(node, "/lan"), headers=admin, json={"spec": LAN["spec"], "expected_sha256": sha_now}
    )
    assert stale.status_code == 409  # someone changed it since
    now = client.get(url(node), headers=admin).json()["middlewares"][0]["sha256"]
    assert client.delete(url(node, f"/lan?expected_sha256={now}"), headers=admin).status_code == 204
    assert not (folder / "middlewares" / "lan.yml").exists()
    log = client.get("/api/v1/audit", headers=admin).text
    assert "traefik.middleware_create" in log and "traefik.middleware_delete" in log


def test_passwords_never_come_back_or_reach_the_audit_log(hub):
    client, admin, node, folder = hub
    assert client.post(url(node), headers=admin, json=AUTH).status_code == 201
    text = (folder / "middlewares" / "private.yml").read_text()
    assert "s3cret-pass-phrase" not in text and "$2y$12$" in text
    listed = client.get(url(node), headers=admin)
    assert "s3cret-pass-phrase" not in listed.text and "$2y$" not in listed.text
    assert listed.json()["middlewares"][0]["spec"]["users"] == [
        {"username": "alice", "has_password": True}
    ]
    assert "s3cret-pass-phrase" not in client.get("/api/v1/audit", headers=admin).text


def test_changing_a_user_without_a_new_password_keeps_the_old_one(hub):
    client, admin, node, folder = hub
    client.post(url(node), headers=admin, json=AUTH)
    before = (folder / "middlewares" / "private.yml").read_text()
    sha_now = client.get(url(node), headers=admin).json()["middlewares"][0]["sha256"]
    got = client.put(
        url(node, "/private"),
        headers=admin,
        json={
            "spec": {
                "intent": "basic_auth",
                "name": "private",
                "users": [{"username": "alice"}],
                "remove_header": False,
            },
            "expected_sha256": sha_now,
        },
    )
    assert got.status_code == 200
    after = (folder / "middlewares" / "private.yml").read_text()
    assert (
        before.split("users:")[1].split("\n")[1] == after.split("users:")[1].split("\n")[1]
    )  # same hash
    new_user = client.put(
        url(node, "/private"),
        headers=admin,
        json={
            "spec": {
                "intent": "basic_auth",
                "name": "private",
                "users": [{"username": "alice"}, {"username": "bob"}],
            },
            "expected_sha256": client.get(url(node), headers=admin).json()["middlewares"][0][
                "sha256"
            ],
        },
    )
    assert (
        new_user.status_code == 422
        and new_user.json()["error"]["code"] == "traefik.password_needed"
    )


def test_a_name_in_use_a_hand_edited_file_and_a_kind_change_are_refused(hub):
    client, admin, node, folder = hub
    (folder / "extra.yml").write_text("http:\n  middlewares:\n    lan:\n      compress: {}\n")
    taken = client.post(url(node), headers=admin, json=LAN)
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "traefik.name_taken"
    client.post(url(node), headers=admin, json={"spec": {"intent": "compress", "name": "zip"}})
    item = client.get(url(node), headers=admin).json()["middlewares"][0]
    kind = client.put(
        url(node, "/zip"),
        headers=admin,
        json={"spec": {"intent": "rate_limit", "name": "zip"}, "expected_sha256": item["sha256"]},
    )
    assert kind.status_code == 422
    path = folder / "middlewares" / "zip.yml"
    path.write_text(path.read_text() + "# hand edit\n")
    item = client.get(url(node), headers=admin).json()["middlewares"][0]
    assert item["editable"] is False
    edited = client.put(
        url(node, "/zip"),
        headers=admin,
        json={"spec": {"intent": "compress", "name": "zip"}, "expected_sha256": item["sha256"]},
    )
    assert (
        edited.status_code == 409 and edited.json()["error"]["code"] == "traefik.modified_by_hand"
    )


def test_a_middleware_in_use_cannot_be_deleted(hub):
    client, admin, node, folder = hub
    client.post(url(node), headers=admin, json=LAN)
    (folder / "site.yml").write_text(
        "http:\n  routers:\n    blog:\n      rule: Host(`a.example.com`)\n"
        "      service: s\n      middlewares: [lan@file]\n"
    )
    listed = client.get(url(node), headers=admin).json()["middlewares"][0]
    assert listed["used_by"] == ["blog"]
    got = client.delete(url(node, f"/lan?expected_sha256={listed['sha256']}"), headers=admin)
    assert got.status_code == 409 and got.json()["error"]["code"] == "traefik.middleware_in_use"
    assert (folder / "middlewares" / "lan.yml").exists()


def test_preview_shows_the_file_without_writing_or_hashing(hub):
    client, admin, node, folder = hub
    got = client.post(url(node, "/preview"), headers=admin, json=AUTH)
    assert got.status_code == 200 and got.json()["reference"] == "private@file"
    assert "s3cret-pass-phrase" not in got.text and not (folder / "middlewares").exists()


def test_rights_and_visibility(hub):
    client, admin, node, folder = hub
    scope = f"node:{node}"

    def person(name, grants):
        uid = client.post(
            "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
        ).json()["id"]
        for right in grants:
            client.post(
                f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
            )
        token = client.post(
            "/api/v1/auth/login", json={"username": name, "password": PASSWORD}
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    reader = person("reader", ["nodes.read", "traefik.read"])
    stranger = person("stranger", [])
    assert client.get(url(node), headers=reader).status_code == 200
    assert client.post(url(node), headers=reader, json=LAN).status_code == 403
    assert client.get(url(node), headers=stranger).status_code == 404
    assert client.post(url(node), headers=stranger, json=LAN).status_code == 404
    assert (
        client.delete(url(node, "/lan?expected_sha256=" + "0" * 64), headers=reader).status_code
        == 403
    )


@pytest.mark.parametrize("name", ["../x", "A", "a b", "", None, 5, "x" * 41, "a/b", ".hidden"])
def test_middleware_names_are_checked_by_the_worker_too(folder, name):
    with pytest.raises(OperationError):
        traefik_ops.middleware_write({"name": name, "content": "a: 1\n", "expected_sha256": None})


def test_the_middleware_folder_is_made_but_a_linked_one_is_refused(folder, tmp_path):
    traefik_ops.middleware_write({"name": "ok", "content": "a: 1\n", "expected_sha256": None})
    assert (folder / "middlewares" / "ok.yml").read_text() == "a: 1\n"
    other = tmp_path / "other"
    other.mkdir()
    (folder / "middlewares").rename(folder / "moved")
    (folder / "middlewares").symlink_to(other)
    with pytest.raises(OperationError):
        traefik_ops.middleware_write({"name": "bad", "content": "a: 1\n", "expected_sha256": None})
    assert list(other.iterdir()) == []


def test_a_previous_version_goes_to_the_history_when_a_middleware_changes(folder):
    traefik_ops.middleware_write({"name": "lan", "content": "a: 1\n", "expected_sha256": None})
    traefik_ops.middleware_write(
        {"name": "lan", "content": "a: 2\n", "expected_sha256": sha("a: 1\n")}
    )
    from armadok.worker import sites_ops

    assert len(sites_ops.history_list({"path": "middlewares/lan.yml"})["versions"]) == 1


# --- review points ----------------------------------------------------------------------------

ALL_SPECS = [
    {"intent": "local_only", "name": "a"},
    {
        "intent": "basic_auth",
        "name": "b",
        "users": [{"username": "u", "password": "long-enough-pass"}],
    },
    {"intent": "security_headers", "name": "c", "csp": "default-src 'self'; img-src data:"},
    {"intent": "redirect_https", "name": "d"},
    {"intent": "strip_prefix", "name": "e", "prefixes": ["/app"]},
    {"intent": "rate_limit", "name": "f"},
    {"intent": "compress", "name": "g"},
    {"intent": "forward_auth", "name": "h", "address": "https://auth.example.com/verify"},
]


@pytest.mark.parametrize("raw", ALL_SPECS)
def test_no_generated_file_contains_a_template_marker(raw):
    # Traefik runs Go templates on its files: "{{" must never be producible.
    text = mw.render(mw.resolve(make(**raw)))
    assert "{{" not in text and "}}" not in text


def test_a_hash_with_non_ascii_digits_is_refused():
    fake = "$2y$\u0661\u0662$" + "a" * 53
    with pytest.raises(ValidationError):
        make(intent="basic_auth", name="x", users=[{"username": "u", "hash": fake}])


def test_a_user_without_any_hash_is_never_written():
    spec = make(
        intent="basic_auth", name="x", users=[{"username": "u", "password": "long-enough-pass"}]
    )
    with pytest.raises(ValueError):
        mw.render(spec)  # not resolved: no hash yet


def test_everything_about_the_passwords_is_checked_before_any_hashing(monkeypatch):
    monkeypatch.setattr(mw, "hash_password", lambda p: pytest.fail("must not hash"))
    too_many = make(
        intent="basic_auth",
        name="x",
        users=[
            {"username": f"u{i}", "password": "long-enough-pass"}
            for i in range(mw.MAX_NEW_PASSWORDS + 1)
        ],
    )
    with pytest.raises(ValueError, match="too many"):
        mw.resolve(too_many)
    long_last = make(
        intent="basic_auth",
        name="x",
        users=[
            {"username": "a", "password": "long-enough-pass"},
            {"username": "b", "password": "é" * 40},
        ],
    )
    with pytest.raises(ValueError, match="too long"):  # 80 bytes, 40 characters
        mw.resolve(long_last)


def person(client, admin, node, name, grants):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right in grants:
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_changing_a_middleware_takes_the_sensitive_sites_right(hub):
    client, admin, node, folder = hub
    client.post(url(node), headers=admin, json=LAN)
    editor = person(
        client,
        admin,
        node,
        "editor",
        ["nodes.read", "traefik.read", "traefik.files.read", "traefik.files.write"],
    )
    item = client.get(url(node), headers=admin).json()["middlewares"][0]
    assert (
        client.post(
            url(node), headers=editor, json={"spec": {"intent": "compress", "name": "zip"}}
        ).status_code
        == 403
    )
    assert client.post(url(node, "/preview"), headers=editor, json=LAN).status_code == 403
    widen = {
        "spec": {"intent": "local_only", "name": "lan", "ranges": ["0.0.0.0/0"]},
        "expected_sha256": item["sha256"],
    }
    assert client.put(url(node, "/lan"), headers=editor, json=widen).status_code == 403
    assert (
        client.delete(
            url(node, f"/lan?expected_sha256={item['sha256']}"), headers=editor
        ).status_code
        == 403
    )
    # Nor can the plain file editor reach the same files by another road.
    generic = client.put(
        f"/api/v1/nodes/{node}/traefik/files/middlewares/lan.yml",
        headers=editor,
        json={"content": "http: {}\n", "expected_sha256": item["sha256"]},
    )
    assert generic.status_code == 403
    assert "0.0.0.0/0" not in (folder / "middlewares" / "lan.yml").read_text()


def test_a_key_cannot_hold_the_right_to_change_middlewares(hub):
    client, admin, node, folder = hub
    got = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["traefik.sites.write"]}
    )
    assert got.status_code == 422 and got.json()["error"]["code"] == "apikeys.invalid_right"


def test_the_details_of_a_middleware_are_for_those_who_may_read_files(hub):
    client, admin, node, folder = hub
    client.post(url(node), headers=admin, json=AUTH)
    viewer = person(client, admin, node, "viewer", ["nodes.read", "traefik.read"])
    shown = client.get(url(node), headers=viewer).json()["middlewares"][0]
    assert shown["spec"] == {"intent": "basic_auth", "name": "private"} and "alice" not in str(
        shown
    )
    full = client.get(url(node), headers=admin).json()["middlewares"][0]
    assert full["spec"]["users"][0]["username"] == "alice"


def test_a_file_edited_by_hand_or_posing_as_another_cannot_be_deleted_through_the_form(hub):
    client, admin, node, folder = hub
    client.post(url(node), headers=admin, json=LAN)
    path = folder / "middlewares" / "lan.yml"
    path.write_text(path.read_text() + "  extra:\n    compress: {}\n")
    item = client.get(url(node), headers=admin).json()["middlewares"][0]
    got = client.delete(url(node, f"/lan?expected_sha256={item['sha256']}"), headers=admin)
    assert got.status_code == 409 and got.json()["error"]["code"] == "traefik.modified_by_hand"
    assert path.exists()
    # A file whose marker names another middleware than its own file name.
    other = mw.render(make(intent="compress", name="zip"))
    (folder / "middlewares" / "liar.yml").write_text(other)
    digest = hashlib.sha256(other.encode()).hexdigest()
    got = client.delete(url(node, f"/liar?expected_sha256={digest}"), headers=admin)
    assert got.status_code == 409 and (folder / "middlewares" / "liar.yml").exists()


def test_hashing_happens_off_the_event_loop(hub, monkeypatch):
    import threading

    client, admin, node, folder = hub
    seen = []
    real = mw.resolve

    def spy(spec, previous=None):
        seen.append(threading.current_thread().name)
        return real(spec, previous)

    monkeypatch.setattr(mw, "resolve", spy)
    assert client.post(url(node), headers=admin, json=AUTH).status_code == 201
    assert seen and not any(name == "MainThread" for name in seen)


def test_too_many_passwords_in_one_request_are_refused_quickly(hub):
    client, admin, node, folder = hub
    users = [
        {"username": f"u{i}", "password": "long-enough-pass"}
        for i in range(mw.MAX_NEW_PASSWORDS + 1)
    ]
    got = client.post(
        url(node),
        headers=admin,
        json={"spec": {"intent": "basic_auth", "name": "many", "users": users}},
    )
    assert got.status_code == 422 and got.json()["error"]["code"] == "traefik.too_many_passwords"
    assert not (folder / "middlewares").exists()


def test_a_rejected_request_does_not_echo_what_was_sent(hub):
    client, admin, node, folder = hub
    bad = {
        "spec": {
            "intent": "basic_auth",
            "name": "x",
            "users": [{"username": "a", "password": "short"}],
        }
    }
    got = client.post(url(node), headers=admin, json=bad)
    assert got.status_code == 422 and "short" not in got.text
    assert got.json()["error"]["code"] == "common.invalid_request" and got.json()["error"]["fields"]


def test_an_update_is_audited_with_counts_not_values(hub):
    client, admin, node, folder = hub
    client.post(url(node), headers=admin, json=AUTH)
    item = client.get(url(node), headers=admin).json()["middlewares"][0]
    client.put(
        url(node, "/private"),
        headers=admin,
        json={
            "spec": {
                "intent": "basic_auth",
                "name": "private",
                "users": [
                    {"username": "alice"},
                    {"username": "bob", "password": "another-long-pass"},
                ],
            },
            "expected_sha256": item["sha256"],
        },
    )
    log = client.get("/api/v1/audit", headers=admin).text
    assert "new_passwords" in log and "bob" not in log and "another-long-pass" not in log
