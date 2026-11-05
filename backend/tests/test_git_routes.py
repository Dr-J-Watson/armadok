"""The hub side of Git sources, with the worker replaced by a recording stand-in."""

import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.errors import ApiError
from armadok.main import create_app
from armadok.modules.git import routes, schemas
from tests.test_members_admin import PASSWORD, login, owner_with_node, person

SHA = "a" * 40
URL = "https://git.example.com/org/blog"
TOKEN = "gh" + "p_" + "abcdef123456"


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
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
    calls = []

    async def ask(request, registry, node_id, op, args, model):
        calls.append((op, args))
        if op == "git.status":
            return schemas.Status(commit=SHA, subject="s", dirty=False, remote="b" * 40)
        if args.get("fail"):
            raise ApiError("worker.git_dirty", 409)
        return schemas.Commit(commit=SHA, subject="s")

    monkeypatch.setattr(routes, "ask", ask)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app, calls


def test_clone_remembers_the_source_and_sends_the_login_only_for_its_host(box):
    client, admin, app, calls = box
    _, olga, node = owner_with_node(client, admin)
    base = f"/api/v1/nodes/{node}/git"
    saved = client.put(
        base + "/logins",
        headers=olga,
        json={"host": "Git.Example.com", "username": "bob", "secret": TOKEN},
    )
    assert saved.status_code == 204
    other = client.put(
        base + "/logins",
        headers=olga,
        json={"host": "other.example.com", "username": "zed", "secret": TOKEN + "x"},
    )
    assert other.status_code == 204
    made = client.post(base + "/clone", headers=olga, json={"name": "blog", "url": URL})
    assert made.status_code == 201 and made.json()["commit"] == SHA
    op, args = calls[-1]
    assert op == "git.clone" and args["login"] == {"username": "bob", "token": TOKEN}
    assert args["branch"] == "main"
    got = client.get(base + "/projects/blog", headers=olga).json()
    assert got["source"]["url"] == URL and got["status"]["remote"] == "b" * 40
    # A repository on a host without a login is asked for anonymously.
    client.post(
        base + "/clone", headers=olga, json={"name": "docs", "url": "https://x.example.org/a/b"}
    )
    assert calls[-1][1]["login"] is None


def test_update_and_forget_follow_the_project_rights_and_are_audited_without_the_login(box):
    client, admin, app, calls = box
    _, olga, node = owner_with_node(client, admin)
    base = f"/api/v1/nodes/{node}/git"
    client.put(
        base + "/logins",
        headers=olga,
        json={"host": "git.example.com", "username": "b", "secret": TOKEN},
    )
    client.post(base + "/clone", headers=olga, json={"name": "blog", "url": URL})
    assert client.post(base + "/projects/blog/update", headers=olga).status_code == 200
    assert calls[-1][0] == "git.pull"
    assert client.post(base + "/projects/none/update", headers=olga).status_code == 404
    pat_id, pat = person(client, admin, "pat")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    )
    assert client.get(base + "/projects/blog", headers=pat).status_code == 200  # may look
    assert client.post(base + "/projects/blog/update", headers=pat).status_code == 403
    assert client.delete(base + "/projects/blog", headers=pat).status_code == 403
    assert client.get(base + "/logins", headers=pat).status_code == 403
    assert client.delete(base + "/projects/blog", headers=olga).status_code == 204
    assert client.get(base + "/projects/blog", headers=olga).json()["source"] is None
    log = client.get("/api/v1/audit", headers=admin).text
    assert "git.update" in log and "git.clone" in log and TOKEN not in log


def test_logins_are_encrypted_never_shown_and_addresses_are_checked(box):
    client, admin, app, calls = box
    _, olga, node = owner_with_node(client, admin)
    base = f"/api/v1/nodes/{node}/git"
    client.put(
        base + "/logins",
        headers=olga,
        json={"host": "git.example.com", "username": "b", "secret": TOKEN},
    )
    assert TOKEN not in client.get(base + "/logins", headers=olga).text
    conn = app.state.settings.open_db()
    stored = conn.execute("SELECT secret FROM git_logins").fetchone()["secret"]
    conn.close()
    assert TOKEN not in stored
    bad = [
        "http://git.example.com/a/b",
        "https://u:p" + "@git.example.com/a/b",
        "ssh://git" + "@git.example.com/a/b",
        "https://git.example.com/a b",
        "https://git.example.com/",
    ]
    for url in bad:
        assert (
            client.post(base + "/clone", headers=olga, json={"name": "x", "url": url}).status_code
            == 422
        )
    for branch in ("--upload-pack=x", "a..b", " x"):
        body = {"name": "x", "url": URL, "branch": branch}
        assert client.post(base + "/clone", headers=olga, json=body).status_code == 422
    assert (
        client.post(base + "/clone", headers=olga, json={"name": "../x", "url": URL}).status_code
        == 422
    )
    assert not [c for c in calls if c[0] == "git.clone"]
    assert client.delete(base + "/logins/git.example.com", headers=olga).status_code == 204
    assert client.delete(base + "/logins/git.example.com", headers=olga).status_code == 404
