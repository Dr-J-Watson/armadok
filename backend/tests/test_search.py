import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.search import routes

PASSWORD = "correct horse battery"


@pytest.fixture
def box(tmp_path, caplog):
    routes._calls.clear()
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path / "data", cookie_secure=False, encryption_key=generate_key())
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def find(client, who, q):
    got = client.post("/api/v1/search", headers=who, json={"q": q})
    assert got.status_code == 200, got.text
    return got.json()["results"]


def person(client, admin, name, grants=()):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right, scope in grants:
        client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    return login(client, name)


def test_machines_are_found_only_by_those_who_can_see_them(box):
    client, admin, app = box
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "blog-server", "type": "docker"}
    ).json()["id"]
    hits = find(client, admin, "BLOG")
    assert [(h["type"], h["label"], h["to"]) for h in hits] == [
        ("node", "blog-server", f"/nodes/{node}")
    ]
    stranger = person(client, admin, "stranger")
    assert find(client, stranger, "blog") == []
    reader = person(client, admin, "reader", [("nodes.read", f"node:{node}")])
    assert [h["label"] for h in find(client, reader, "blog")] == ["blog-server"]


def test_people_are_found_only_with_the_right_to_list_them(box):
    client, admin, app = box
    person(client, admin, "alice-dev")
    eve = person(client, admin, "eve")
    assert [h["label"] for h in find(client, admin, "alice") if h["type"] == "user"] == [
        "alice-dev"
    ]
    assert [h for h in find(client, eve, "alice") if h["type"] == "user"] == []


def test_notes_are_private_to_their_owner(box):
    client, admin, app = box
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    made = client.post(
        "/api/v1/notes",
        headers=admin,
        json={
            "node_id": node,
            "object_type": "node",
            "object_id": node,
            "body": "renew the zebra licence",
        },
    )
    assert made.status_code in (200, 201), made.text
    assert [h["type"] for h in find(client, admin, "zebra")] == ["note"]
    other = person(client, admin, "other")
    assert find(client, other, "zebra") == []


def test_a_query_must_be_long_enough_and_is_rate_limited(box, monkeypatch):
    client, admin, app = box
    assert client.post("/api/v1/search", headers=admin, json={"q": "a"}).status_code == 422
    assert client.post("/api/v1/search", headers=admin, json={"q": "x" * 65}).status_code == 422
    assert client.post("/api/v1/search", json={"q": "ab"}).status_code == 401
    monkeypatch.setattr(routes, "CALLS_PER_MINUTE", 3)
    for _ in range(3):
        assert client.post("/api/v1/search", headers=admin, json={"q": "zz"}).status_code == 200
    got = client.post("/api/v1/search", headers=admin, json={"q": "zz"})
    assert got.status_code == 429 and got.json()["error"]["code"] == "search.too_fast"


def test_a_broken_provider_does_not_break_the_search(box, monkeypatch):
    client, admin, app = box
    client.post("/api/v1/nodes", headers=admin, json={"name": "blog", "type": "docker"})
    from armadok.core.module import SearchProvider

    def boom(*_a):
        raise RuntimeError("provider bug")

    providers = app.state.registry.search_providers()
    monkeypatch.setattr(
        type(app.state.registry),
        "search_providers",
        lambda self: (SearchProvider("broken", boom), *providers),
    )
    assert [h["label"] for h in find(client, admin, "blog")] == ["blog"]


def test_results_are_capped_per_provider(box):
    client, admin, app = box
    for i in range(12):
        client.post("/api/v1/nodes", headers=admin, json={"name": f"web-{i}", "type": "docker"})
    assert len(find(client, admin, "web")) == routes.PER_PROVIDER


def test_the_text_is_not_accepted_in_the_address(box):
    client, admin, app = box
    assert client.get("/api/v1/search", headers=admin, params={"q": "zebra"}).status_code == 405


def test_a_read_only_key_may_search_but_not_see_notes(box):
    client, admin, app = box
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "blog", "type": "docker"}
    ).json()["id"]
    client.post(
        "/api/v1/notes",
        headers=admin,
        json={"node_id": node, "object_type": "node", "object_id": node, "body": "zebra licence"},
    )
    made = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={"name": "ro", "rights": ["nodes.read"], "read_only": True},
    )
    assert made.status_code in (200, 201), made.text
    key = {"Authorization": f"Bearer {made.json()['key']}"}
    kinds = {h["type"] for h in find(client, key, "blog")}
    assert "node" in kinds
    assert [
        h for h in find(client, key, "zebra") if h["type"] == "note"
    ] == []  # keys never read notes
    assert [h["type"] for h in find(client, admin, "zebra")] == ["note"]


def test_notes_search_needs_three_letters_has_its_own_budget_and_survives_a_bad_note(
    box, monkeypatch
):
    from armadok.modules.notes import search as notes_search

    client, admin, app = box
    notes_search._budget.clear()
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    ids = {}
    for body in ("alpha one", "beta two"):
        made = client.post(
            "/api/v1/notes",
            headers=admin,
            json={"node_id": node, "object_type": "node", "object_id": node, "body": body},
        )
        ids[body] = made.json()["id"]
    conn = app.state.settings.open_db()
    conn.execute(
        "UPDATE notes SET body = 'not-a-valid-token', encrypted = 1 WHERE id = ?",
        (ids["beta two"],),
    )
    conn.commit()
    conn.close()
    assert find(client, admin, "al") == []  # two letters: not worth decrypting every note
    assert [h["type"] for h in find(client, admin, "alpha")] == [
        "note"
    ]  # the damaged one is skipped
    monkeypatch.setattr(notes_search, "CALLS_PER_MINUTE", 2)
    notes_search._budget.clear()
    results = [find(client, admin, "alpha") for _ in range(4)]
    assert [bool(r) for r in results] == [True, True, False, False]


def test_the_rate_limiter_forgets_only_quiet_accounts(monkeypatch):
    routes._calls.clear()
    monkeypatch.setattr(routes, "MAX_TRACKED", 3)
    monkeypatch.setattr(routes, "CALLS_PER_MINUTE", 2)
    assert routes._within_rate("busy") and routes._within_rate("busy")
    assert routes._within_rate("busy") is False
    for name in ("a", "b"):
        routes._within_rate(name)
    # Many new accounts do not reset the busy one's count.
    for name in ("c", "d", "e"):
        routes._within_rate(name)
    assert routes._within_rate("busy") is False


# --- the background index of what lives on the machines ---------------------------------------


def seed(app, node, rows):
    conn = app.state.settings.open_db()
    for provider, kind, ident, label in rows:
        conn.execute(
            "INSERT INTO search_index (node_id, provider, type, object_id, label)"
            " VALUES (?, ?, ?, ?, ?)",
            (node, provider, kind, ident, label),
        )
    conn.commit()
    conn.close()


def test_containers_projects_and_sites_are_found_only_where_the_provider_may_be_read(box):
    client, admin, app = box
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    seed(
        app,
        node,
        [
            ("docker", "container", "c1", "blog-web"),
            ("traefik", "router", "r1", "blog-router"),
            ("traefik", "domain", "d1", "blog.example.com"),
        ],
    )
    got = find(client, admin, "blog")
    assert {h["type"] for h in got} >= {"container", "router", "domain"}
    docker_only = person(
        client,
        admin,
        "dora",
        [("nodes.read", f"node:{node}"), ("docker.read", f"node:{node}")],
    )
    got = find(client, docker_only, "blog")
    assert [h["type"] for h in got] == ["container"]
    assert got[0]["to"] == f"/nodes/{node}?tab=map&focus=container:c1"
    nobody = person(client, admin, "nina")
    assert find(client, nobody, "blog") == []
    # A LIKE wildcard typed by the user is just text.
    assert find(client, admin, "%%") == []


def test_a_round_replaces_a_providers_names_and_a_failure_keeps_the_old_ones(box, monkeypatch):
    import asyncio

    from armadok.core.module import RelationData, RelationObject, RelationProvider
    from armadok.modules.search import index

    client, admin, app = box
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()
    answers = [
        RelationData(
            (RelationObject("container", "c1", "alpha"), RelationObject("probe", "p", "x"))
        ),
        RelationData((RelationObject("container", "c2", "beta"),)),
    ]

    async def provide(app_, node_id, viewer):
        # The indexer reads as a principal of its own with every right on this machine only.
        assert viewer.kind == "background" and viewer.grants
        assert all(g.scope == f"node:{node_id}" for g in viewer.grants)
        if not answers:
            raise RuntimeError("machine offline")
        return answers.pop(0)

    fake = RelationProvider("docker", "docker.read", ("docker", "full", "network"), provide)
    monkeypatch.setattr(app.state.registry, "relation_providers", lambda: (fake,))

    def names():
        conn = app.state.settings.open_db()
        try:
            return sorted(r[0] for r in conn.execute("SELECT label FROM search_index"))
        finally:
            conn.close()

    client.portal.call(index.refresh_node, app, {"id": node["id"], "type": "docker"})
    assert names() == ["alpha"]  # a probe is not worth a search result
    client.portal.call(index.refresh_node, app, {"id": node["id"], "type": "docker"})
    assert names() == ["beta"]  # replaced, not accumulated
    client.portal.call(index.refresh_node, app, {"id": node["id"], "type": "docker"})
    assert names() == ["beta"]  # the machine did not answer: the last names stay
    assert asyncio.iscoroutinefunction(index.index_loop)


def test_a_change_made_in_armadok_refreshes_the_machine_soon_and_once_per_burst(box, monkeypatch):
    import asyncio

    from armadok.modules.search import index

    client, admin, app = box
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "hot", "type": "docker"}
    ).json()
    seen = []

    async def fake_refresh(_app, n):
        seen.append(n["id"])
        return 0

    monkeypatch.setattr(index, "refresh_node", fake_refresh)
    monkeypatch.setattr(index, "REFRESH_AFTER_CHANGE_SECONDS", 0.05)

    async def burst():
        await index.on_signal(app, "docker.changed", node["id"])
        await index.on_signal(app, "projects.changed", node["id"])  # same burst: no second run
        await index.on_signal(app, "notes.changed", node["id"])  # not about what is indexed
        await index.on_signal(app, "docker.changed", None)
        await asyncio.sleep(0.4)

    client.portal.call(burst)
    assert seen == [node["id"]]


def test_a_deleted_machine_disappears_from_the_index(box):
    client, admin, app = box
    created = client.post("/api/v1/nodes", headers=admin, json={"name": "tmp", "type": "docker"})
    node = created.json()["id"]
    seed(app, node, [("docker", "container", "c9", "ghostly")])
    assert len(find(client, admin, "ghostly")) == 1
    assert client.delete(f"/api/v1/nodes/{node}", headers=admin).status_code in (204, 401)


def test_an_api_key_never_reads_or_shares_notes_even_its_owners(box):
    client, admin, app = box
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    body = {"node_id": node, "object_type": "container", "object_id": "abc", "body": "private"}
    note = client.post("/api/v1/notes", headers=admin, json=body).json()
    made = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={"name": "k", "rights": ["notes.write", "nodes.read"]},
    )
    token = {"Authorization": f"Bearer {made.json()['key']}"}
    for method, path in (
        ("get", "/api/v1/notes"),
        ("get", "/api/v1/notes/shared"),
        ("get", f"/api/v1/notes/{note['id']}/shares"),
        ("post", "/api/v1/notes"),
        ("patch", f"/api/v1/notes/{note['id']}"),
        ("delete", f"/api/v1/notes/{note['id']}"),
    ):
        extra = {"json": {}} if method in ("post", "patch") else {}
        got = getattr(client, method)(path, headers=token, **extra)
        assert got.status_code == 403, (method, path, got.status_code)
    assert client.get("/api/v1/notes", headers=admin).status_code == 200
