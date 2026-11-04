"""Public status pages: managed with the probe's right, read by anyone with the address."""

import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.main import create_app
from armadok.modules.status import routes
from armadok.modules.uptime import repository as probes
from tests.test_members_admin import PASSWORD, login, owner_with_node, person


@pytest.fixture
def box(tmp_path, caplog):
    routes._cache.clear()
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False, local_node=True))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin")


def a_probe(client, owner, node):
    made = client.post(
        f"/api/v1/nodes/{node}/uptime",
        headers=owner,
        json={"name": "internal-db-host", "kind": "http", "url": "https://example.com/health"},
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def test_a_page_shows_only_a_title_a_state_and_dots_and_closes_for_good(box):
    client, admin = box
    _, olga, node = owner_with_node(client, admin)
    probe = a_probe(client, olga, node)
    url = f"/api/v1/nodes/{node}/uptime/{probe}/public"
    assert client.get(url, headers=olga).json()["published"] is False
    page = client.put(url, headers=olga, json={"title": "Blog"})
    assert page.status_code == 200
    slug = page.json()["slug"]
    assert len(slug) >= 20

    conn = client.app.state.settings.open_db()
    for ok in (True, True, False, True):
        probes.record_result(conn, probe, ok, 12, None if ok else "connection refused to 10.0.0.5")
    probes.set_state(conn, probe, "up", 0, 3, None)
    conn.close()

    public = client.get(f"/api/v1/status/{slug}")  # no sign-in
    assert public.status_code == 200
    body = public.json()
    assert set(body) == {"title", "state", "since", "checked_at", "availability", "recent"}
    assert body["title"] == "Blog" and body["state"] == "up"
    assert body["recent"] == [True, True, False, True] and body["availability"] == 75.0
    text = public.text
    for secret in ("internal-db-host", "example.com", "10.0.0.5", node, probe):
        assert secret not in text
    assert "noindex" in public.headers["x-robots-tag"]

    assert client.delete(url, headers=olga).status_code == 204
    routes._cache.clear()
    assert client.get(f"/api/v1/status/{slug}").status_code == 404
    # Opening it again gives a new address, not the old one back.
    again = client.put(url, headers=olga, json={"title": "Blog"}).json()["slug"]
    assert again != slug


def test_only_the_probes_manager_opens_a_page_and_wrong_addresses_are_slowed(box, monkeypatch):
    client, admin = box
    _, olga, node = owner_with_node(client, admin)
    probe = a_probe(client, olga, node)
    url = f"/api/v1/nodes/{node}/uptime/{probe}/public"
    _, pat = person(client, admin, "pat")
    assert client.put(url, headers=pat, json={"title": "x"}).status_code == 404  # cannot see it
    assert client.put(url, headers={}, json={"title": "x"}).status_code == 401
    assert client.put(url, headers=olga, json={"title": "bad\ntitle"}).status_code == 422

    monkeypatch.setattr(routes.guess_limiter, "limits", {"ip": 3})
    codes = [client.get("/api/v1/status/" + "a" * 24).status_code for _ in range(6)]
    assert codes[:2] == [404, 404] and codes[-1] == 429  # slowed after a few wrong addresses


def test_a_paused_probe_says_unknown_and_a_deleted_probe_takes_its_page_with_it(box):
    client, admin = box
    _, olga, node = owner_with_node(client, admin)
    probe = a_probe(client, olga, node)
    url = f"/api/v1/nodes/{node}/uptime/{probe}/public"
    slug = client.put(url, headers=olga, json={"title": "Blog"}).json()["slug"]
    conn = client.app.state.settings.open_db()
    probes.set_state(conn, probe, "down", 3, 0, "x")
    conn.close()
    assert client.get(f"/api/v1/status/{slug}").json()["state"] == "down"
    client.patch(f"/api/v1/nodes/{node}/uptime/{probe}", headers=olga, json={"enabled": False})
    routes._cache.clear()
    assert client.get(f"/api/v1/status/{slug}").json()["state"] == "unknown"
    client.delete(f"/api/v1/nodes/{node}/uptime/{probe}", headers=olga)
    routes._cache.clear()
    assert client.get(f"/api/v1/status/{slug}").status_code == 404
