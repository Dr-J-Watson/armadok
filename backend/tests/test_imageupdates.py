"""Image update tracking: references, the registry exchange, the comparison, the endpoints."""

import asyncio
import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core import outbound
from armadok.core.config import Settings
from armadok.main import create_app
from armadok.modules.imageupdates import refs, registry, routes, service
from armadok.worker import docker_ops
from tests.test_members_admin import PASSWORD, login, owner_with_node, person

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


# --- references --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nginx", ("registry-1.docker.io", "library/nginx", "latest")),
        ("nginx:1.27", ("registry-1.docker.io", "library/nginx", "1.27")),
        ("grafana/grafana:11", ("registry-1.docker.io", "grafana/grafana", "11")),
        ("docker.io/library/redis:7", ("registry-1.docker.io", "library/redis", "7")),
        ("ghcr.io/org/app:v2", ("ghcr.io", "org/app", "v2")),
        ("registry.example.com:5000/team/app", ("registry.example.com:5000", "team/app", "latest")),
    ],
)
def test_references_become_registry_repository_and_tag(text, expected):
    assert tuple(refs.parse(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nginx@" + D1,  # pinned by digest: it never changes
        "ghcr.io/Org/App:v2",  # upper case is not a repository name
        "ghcr.io/org/app:bad tag",
        "evil.com/../../x:1",
        "a" * 300,
        "ghcr.io/org/app:" + "t" * 200,
        "http://x/y:1",
    ],
)
def test_odd_references_are_not_followed(text):
    assert refs.parse(text) is None


# --- the registry exchange ---------------------------------------


def fake_fetch(monkeypatch, handler):
    calls = []

    async def fetch(method, url, headers, *, allow_private=False, max_bytes=65536):
        calls.append((method, url, dict(headers)))
        return handler(method, url, headers)

    monkeypatch.setattr(outbound, "fetch", fetch)
    return calls


def test_an_anonymous_token_is_fetched_when_the_registry_asks_for_one(monkeypatch):
    def handler(method, url, headers):
        if url.startswith("https://auth.example.com/token"):
            return 200, {}, b'{"token": "abc"}'
        if "Authorization" not in headers:
            challenge = 'Bearer realm="https://auth.example.com/token",service="registry"'
            return 401, {"www-authenticate": challenge}, b""
        return 200, {"docker-content-digest": D1}, b""

    calls = fake_fetch(monkeypatch, handler)
    assert asyncio.run(registry.current_digest(refs.parse("ghcr.io/org/app:v2"))) == D1
    assert [c[0] for c in calls] == ["HEAD", "GET", "HEAD"]
    assert "scope=repository%3Aorg%2Fapp%3Apull" in calls[1][1]
    assert calls[2][2]["Authorization"] == "Bearer abc"


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (404, {}, "images.not_found"),
        (403, {}, "images.denied"),
        (500, {}, "images.registry_unreachable"),
        (200, {}, "images.registry_unreachable"),
        (200, {"docker-content-digest": "sha256:short"}, "images.registry_unreachable"),
    ],
)
def test_a_missing_or_malformed_answer_is_an_error_never_a_guess(
    monkeypatch, status, headers, code
):
    fake_fetch(monkeypatch, lambda m, u, h: (status, headers, b""))
    with pytest.raises(registry.RegistryError) as caught:
        asyncio.run(registry.current_digest(refs.parse("ghcr.io/org/app:v2")))
    assert caught.value.code == code


def test_a_token_service_that_is_not_https_is_refused(monkeypatch):
    challenge = 'Bearer realm="http://169.254.169.254/token"'
    fake_fetch(monkeypatch, lambda m, u, h: (401, {"www-authenticate": challenge}, b""))
    with pytest.raises(registry.RegistryError) as caught:
        asyncio.run(registry.current_digest(refs.parse("ghcr.io/org/app:v2")))
    assert caught.value.code == "images.denied"


def test_the_outbound_guard_error_comes_back_as_a_code(monkeypatch):
    def handler(m, u, h):
        raise outbound.OutboundError("outbound.address_not_allowed")

    fake_fetch(monkeypatch, handler)
    with pytest.raises(registry.RegistryError) as caught:
        asyncio.run(registry.current_digest(refs.parse("internal.example.com/app:1")))
    assert caught.value.code == "outbound.address_not_allowed"


def test_fetch_refuses_private_addresses_and_methods_that_write():
    with pytest.raises(outbound.OutboundError):
        asyncio.run(outbound.fetch("HEAD", "https://127.0.0.1/v2/", {}))
    with pytest.raises(outbound.OutboundError):
        asyncio.run(outbound.fetch("POST", "https://example.com/", {}))
    with pytest.raises(outbound.OutboundError):
        asyncio.run(outbound.fetch("GET", "https://user:pw@example.com/", {}))


# --- the worker's side -------------------------------------------


def test_the_worker_lists_named_images_with_their_own_repository_digests(monkeypatch):
    answers = {
        "/images/json": [
            {
                "Id": "sha256:aaa",
                "RepoTags": ["nginx:1.27", "<none>:<none>"],
                "RepoDigests": [f"nginx@{D1}", f"other/repo@{D2}", "nginx@sha256:bad"],
            },
            {"Id": "sha256:bbb", "RepoTags": ["local/app:dev"], "RepoDigests": []},
        ],
        "/containers/json?all=1": [{"ImageID": "sha256:aaa"}],
    }
    monkeypatch.setattr(docker_ops, "docker_get", lambda path: answers[path])
    got = docker_ops.images_digests({})
    assert got["images"] == [
        {"ref": "nginx:1.27", "digests": [D1], "in_use": True},
        {"ref": "local/app:dev", "digests": [], "in_use": False},
    ]


# --- the comparison and the endpoints ----------------------------


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    routes._last.clear()
    service._busy.clear()
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False, local_node=True))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app


def machine_has(monkeypatch, images):
    async def call(app, reg, node_id, op, args, actor=None):
        assert op == "docker.images.digests"
        return {"images": images}

    monkeypatch.setattr(service, "call_operation", call)


def remote_is(monkeypatch, table):
    async def digest(ref, allow_private=False):
        value = table[ref.repo + ":" + ref.tag]
        if isinstance(value, str) and value.startswith("images."):
            raise registry.RegistryError(value)
        return value

    monkeypatch.setattr(registry, "current_digest", digest)


def test_each_image_in_use_is_current_outdated_unknown_or_in_error(box, monkeypatch):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    machine_has(
        monkeypatch,
        [
            {"ref": "nginx:1.27", "digests": [D1], "in_use": True},
            {"ref": "redis:7", "digests": [D1], "in_use": True},
            {"ref": "ghcr.io/org/gone:1", "digests": [D1], "in_use": True},
            {"ref": "local/app:dev", "digests": [], "in_use": True},
            {"ref": "old/leftover:1", "digests": [D1], "in_use": False},  # not in use: skipped
        ],
    )
    remote_is(
        monkeypatch,
        {
            "library/nginx:1.27": D1,
            "library/redis:7": D2,
            "org/gone:1": "images.not_found",
        },
    )
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(service, "emit", fake_emit)
    got = client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    assert got.status_code == 200
    assert got.json()["counts"] == {"current": 1, "update": 1, "unknown": 1, "error": 1}
    listing = client.get(f"/api/v1/nodes/{node}/image-updates", headers=olga).json()
    states = {i["ref"]: (i["state"], i["error"]) for i in listing["images"]}
    assert states == {
        "redis:7": ("update", None),
        "ghcr.io/org/gone:1": ("error", "images.not_found"),
        "local/app:dev": ("unknown", "images.no_digest"),
        "nginx:1.27": ("current", None),
    }
    assert listing["images"][0]["ref"] == "redis:7"  # updates first
    assert [p["params"]["image"] for p in sent] == ["redis:7"]

    # Checking again does not announce the same new version twice.
    routes._last.clear()
    client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    assert len(sent) == 1
    # A newer version later is announced again.
    remote_is(
        monkeypatch,
        {
            "library/nginx:1.27": D1,
            "library/redis:7": "sha256:" + "3" * 64,
            "org/gone:1": "images.not_found",
        },
    )
    routes._last.clear()
    client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    assert len(sent) == 2


def test_rights_and_the_pace_of_manual_checks(box, monkeypatch):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    pat_id, pat = person(client, admin, "pat")
    machine_has(monkeypatch, [])
    url = f"/api/v1/nodes/{node}/image-updates"
    assert client.get(url, headers=pat).status_code == 404  # cannot see the machine
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    )
    assert client.get(url, headers=pat).status_code == 200
    assert client.post(url + "/check", headers=pat).status_code == 403  # a viewer reads only
    assert client.post(url + "/check", headers=olga).status_code == 200
    assert client.post(url + "/check", headers=olga).status_code == 429
    assert "imageupdates.check" in client.get("/api/v1/audit", headers=admin).text


# --- hostile registries and machines ----------------------------------------


def test_a_hostile_token_answer_is_a_refusal_not_a_crash(monkeypatch):
    challenge = 'Bearer realm="https://auth.example.com/token",service="registry"'
    for body in (b"[" * 60000, b'{"token": "caf\xc3\xa9"}', b'{"token": "a b"}', b"[1]", b"nope"):

        def handler(method, url, headers, body=body):
            if url.startswith("https://auth.example.com/token"):
                return 200, {}, body
            return 401, {"www-authenticate": challenge}, b""

        fake_fetch(monkeypatch, handler)
        with pytest.raises(registry.RegistryError) as caught:
            asyncio.run(registry.current_digest(refs.parse("ghcr.io/org/app:v2")))
        assert caught.value.code == "images.denied"


def test_one_odd_registry_does_not_sink_the_check_of_the_others(box, monkeypatch):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    machine_has(
        monkeypatch,
        [
            {"ref": "nginx:1.27", "digests": [D1], "in_use": True},
            {"ref": "redis:7", "digests": [D1], "in_use": True},
        ],
    )

    async def digest(ref, allow_private=False):
        if ref.repo == "library/redis":
            raise ValueError("a registry that misbehaves in an unexpected way")
        return D1

    monkeypatch.setattr(registry, "current_digest", digest)
    got = client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    assert got.status_code == 200 and got.json()["counts"] == {
        "current": 1,
        "update": 0,
        "unknown": 0,
        "error": 1,
    }


def test_too_many_registries_and_a_slow_check_are_bounded(box, monkeypatch):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    monkeypatch.setattr(service, "MAX_REGISTRIES", 2)
    machine_has(
        monkeypatch,
        [{"ref": f"r{i}.example.com/a/b:1", "digests": [D1], "in_use": True} for i in range(4)],
    )
    remote_is(monkeypatch, {"a/b:1": D1})
    got = client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    assert got.json()["counts"] == {"current": 2, "update": 0, "unknown": 2, "error": 0}
    listing = client.get(f"/api/v1/nodes/{node}/image-updates", headers=olga).json()["images"]
    assert sorted(i["error"] or "" for i in listing).count("images.too_many_registries") == 2

    # A check that overruns its deadline is stopped with a clear code, and can be retried.
    routes._last.clear()
    monkeypatch.setattr(service, "DEADLINE_SECONDS", 0.05)

    async def slow(ref, allow_private=False):
        await asyncio.sleep(1)
        return D1

    monkeypatch.setattr(registry, "current_digest", slow)
    late = client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    assert late.status_code == 504 and late.json()["error"]["code"] == "imageupdates.timeout"


def test_a_name_with_odd_characters_is_cleaned_before_it_is_kept(box, monkeypatch):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    machine_has(monkeypatch, [{"ref": "x/y:1\n<script>", "digests": [D1], "in_use": True}])
    remote_is(monkeypatch, {})
    client.post(f"/api/v1/nodes/{node}/image-updates/check", headers=olga)
    refs_shown = [
        i["ref"]
        for i in client.get(f"/api/v1/nodes/{node}/image-updates", headers=olga).json()["images"]
    ]
    assert refs_shown == ["x/y:1??script?"]
