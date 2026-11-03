import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app

PASSWORD = "correct horse battery"
KEY = generate_key()


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    from armadok.modules.diagnostics import routes

    monkeypatch.setattr(routes, "CACHE_SECONDS", 0)  # most tests change the setup between calls
    routes._cache.clear()
    for name in ("PROJECTS_DIR", "TRAEFIK_DIR", "ACME_FILE", "DOCKER_SOCKET"):
        monkeypatch.delenv(f"ARMADOK_{name}", raising=False)
    monkeypatch.delenv("UVICORN_FORWARDED_ALLOW_IPS", raising=False)
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data", cookie_secure=False, local_node=True, encryption_key=KEY
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
        client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    return login(client, name)


def test_the_system_status_describes_the_setup_without_revealing_any_secret(box, monkeypatch):
    client, admin, _, _ = box
    got = client.get("/api/v1/system/status", headers=admin)
    assert got.status_code == 200
    body = got.json()
    assert body["cookie_secure"] is False and body["encryption_key_set"] is True
    assert body["https"] is False and body["proxy_trust"] == "none"
    assert KEY not in got.text
    # What the client says about itself is not believed: only the server's own view counts.
    lying = client.get("/api/v1/system/status", headers={**admin, "X-Forwarded-Proto": "https"})
    assert lying.json()["https"] is False
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "172.18.0.0/16")
    assert client.get("/api/v1/system/status", headers=admin).json()["proxy_trust"] == "restricted"
    monkeypatch.setenv("UVICORN_FORWARDED_ALLOW_IPS", "*")
    assert client.get("/api/v1/system/status", headers=admin).json()["proxy_trust"] == "any"


def test_the_system_status_is_for_administrators(box):
    client, admin, _, _ = box
    assert client.get("/api/v1/system/status").status_code == 401
    assert (
        client.get("/api/v1/system/status", headers=person(client, admin, "eve")).status_code == 403
    )


def test_readiness_tells_what_is_configured_and_what_is_not(box, monkeypatch, tmp_path):
    client, admin, node, _ = box
    checks = client.get(f"/api/v1/nodes/{node}/readiness", headers=admin).json()["checks"]
    assert checks["projects"] == "not_configured" and checks["traefik"] == "not_configured"
    assert checks["certificates"] == "not_configured" and checks["docker"] == "unavailable"
    projects, dyn = tmp_path / "stacks", tmp_path / "dyn"
    projects.mkdir()
    dyn.mkdir()
    acme = tmp_path / "acme.json"
    acme.write_text("{}")
    acme.chmod(0o600)
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("ARMADOK_TRAEFIK_DIR", str(dyn))
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(acme))
    after = client.get(f"/api/v1/nodes/{node}/readiness", headers=admin).json()["checks"]
    assert after["projects"] == "ok" and after["traefik"] == "ok"
    assert after["certificates"] in ("ok", "error")  # the file is read as Traefik certificates


def test_readiness_gives_nothing_but_states(box, monkeypatch, tmp_path):
    client, admin, node, _ = box
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(tmp_path))
    got = client.get(f"/api/v1/nodes/{node}/readiness", headers=admin)
    assert set(got.json()) == {"node", "checks"}
    assert str(tmp_path) not in got.text
    assert set(got.json()["checks"].values()) <= {
        "ok",
        "not_configured",
        "unavailable",
        "worker_offline",
        "not_applicable",
        "timeout",
        "error",
        "no_right",
    }


def test_a_capability_is_only_checked_for_someone_who_may_read_it(box):
    client, admin, _, _ = box
    olga = person(client, admin, "olga", [("nodes.create", "*")])
    mine = client.post(
        "/api/v1/nodes", headers=olga, json={"name": "mine", "type": "docker"}
    ).json()["id"]
    pat = person(client, admin, "pat")
    pat_id = client.get("/api/v1/auth/me", headers=pat).json()["id"]
    client.post(
        f"/api/v1/nodes/{mine}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-auditor"},
    )
    client.post(
        f"/api/v1/nodes/{mine}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    )
    seen = client.get(f"/api/v1/nodes/{mine}/readiness", headers=pat).json()["checks"]
    assert seen["docker"] == "worker_offline"  # a viewer may read it; the worker is not there
    only_nodes = person(client, admin, "ann")
    ann_id = client.get("/api/v1/auth/me", headers=only_nodes).json()["id"]
    custom = client.post(
        "/api/v1/roles", headers=admin, json={"name": "see only", "rights": ["nodes.read"]}
    ).json()["id"]
    client.post(
        f"/api/v1/nodes/{mine}/members", headers=olga, json={"user_id": ann_id, "role_id": custom}
    )
    limited = client.get(f"/api/v1/nodes/{mine}/readiness", headers=only_nodes)
    assert limited.status_code in (200, 404)
    if limited.status_code == 200:
        assert set(limited.json()["checks"].values()) == {"no_right"}


def test_readiness_follows_the_node_type_and_visibility(box):
    client, admin, _, _ = box
    network = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "net", "type": "network"}
    ).json()["id"]
    checks = client.get(f"/api/v1/nodes/{network}/readiness", headers=admin).json()["checks"]
    assert checks["docker"] == "not_applicable" and checks["projects"] == "not_applicable"
    assert checks["traefik"] == "worker_offline"
    stranger = person(client, admin, "eve")
    assert client.get(f"/api/v1/nodes/{network}/readiness", headers=stranger).status_code == 404
    assert client.get("/api/v1/nodes/nope/readiness", headers=admin).status_code == 404


def test_a_suspended_node_is_not_probed(box):
    client, admin, _, _ = box
    import time

    from armadok.modules.users import totp

    olga = person(client, admin, "olga", [("nodes.create", "*")])
    mine = client.post(
        "/api/v1/nodes", headers=olga, json={"name": "Olga box", "type": "docker"}
    ).json()["id"]
    secret = client.post(
        "/api/v1/auth/2fa/enroll", headers=admin, json={"password": PASSWORD}
    ).json()["secret"]
    codes = client.post(
        "/api/v1/auth/2fa/confirm",
        headers=admin,
        json={"code": totp.code_at(secret, int(time.time() // 30))},
    ).json()["backup_codes"]
    client.post(
        f"/api/v1/admin/nodes/{mine}/suspend",
        headers=admin,
        json={"reason": "Under review by the host", "confirm_name": "Olga box", "code": codes[0]},
    )
    assert client.get(f"/api/v1/nodes/{mine}/readiness", headers=olga).status_code == 404


def test_a_reloaded_page_asks_the_worker_once_and_never_too_many_at_a_time(box, monkeypatch):
    import asyncio

    from armadok.modules.diagnostics import routes

    client, admin, node, _ = box
    monkeypatch.setattr(routes, "CACHE_SECONDS", 60)
    live = {"now": 0, "max": 0, "calls": 0}

    async def slow(app, registry, node_id, op, args, actor=None):
        live["calls"] += 1
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return {"version": "1", "projects": [], "files": [], "certificates": []}

    monkeypatch.setattr(routes, "call_operation", slow)
    first = client.get(f"/api/v1/nodes/{node}/readiness", headers=admin).json()["checks"]
    assert set(first.values()) == {"ok"} and live["calls"] == 4
    assert live["max"] <= routes.MAX_CONCURRENT_CHECKS
    for _ in range(5):
        client.get(f"/api/v1/nodes/{node}/readiness", headers=admin)
    assert live["calls"] == 4  # the five reloads were answered from the last result


def test_an_answer_of_the_wrong_shape_is_not_taken_for_ok(box, monkeypatch):
    from armadok.modules.diagnostics import routes

    client, admin, node, _ = box

    async def liar(app, registry, node_id, op, args, actor=None):
        return {"anything": "at all"}

    monkeypatch.setattr(routes, "call_operation", liar)
    checks = client.get(f"/api/v1/nodes/{node}/readiness", headers=admin).json()["checks"]
    assert set(checks.values()) == {"error"}
