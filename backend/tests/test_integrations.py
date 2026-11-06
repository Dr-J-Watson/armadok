import json
import logging
import re
import sqlite3
import stat
import time

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.hooks import routes as hook_routes

PASSWORD = "correct horse battery"
FAKE = """#!/bin/sh
DIR=$(dirname "$0")
echo "$@" >> "$DIR/calls"
echo "Container web Started"
"""


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    stacks = tmp_path / "stacks"
    (stacks / "blog").mkdir(parents=True)
    (stacks / "blog" / "compose.yaml").write_text("services: {}\n")
    fake = tmp_path / "docker"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(stacks))
    monkeypatch.setenv("ARMADOK_DOCKER_BIN", str(fake))
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", str(tmp_path / "none.sock"))
    from armadok.worker import compose_ops

    compose_ops._tasks.clear()
    hook_routes.limiter.clear()
    monkeypatch.setattr(hook_routes, "MIN_INTERVAL_SECONDS", 0)
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
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
        from armadok.modules.projects import states

        conn = app.state.settings.open_db()
        states.set_state(conn, node, ["blog"], "managed")  # imported, as an owner would have done
        conn.close()
        yield client, admin, node, tmp_path
    compose_ops._tasks.clear()


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def new_hook(client, headers, node, **over):
    body = {
        "name": "CI",
        "action": "project.redeploy",
        "target": {"node_id": node, "project": "blog"},
        **over,
    }
    return client.post("/api/v1/hooks", headers=headers, json=body)


def fire(client, hook_id, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/api/v1/hooks/{hook_id}/fire", headers=headers)


def wait_calls(tmp, seconds=8):
    for _ in range(seconds * 20):
        if (tmp / "calls").exists():
            return (tmp / "calls").read_text()
        time.sleep(0.05)
    return ""


# --- incoming hooks -------------------------------------------------------------------------


def test_a_hook_redeploys_one_project_without_any_login(box):
    client, admin, node, tmp = box
    made = new_hook(client, admin, node)
    assert made.status_code == 201
    hook = made.json()
    assert hook["fire_path"] == f"/api/v1/hooks/{hook['id']}/fire"
    answer = fire(client, hook["id"], hook["token"])  # no session, no key: the token alone
    assert answer.status_code == 202 and answer.json() == {"started": True}
    calls = wait_calls(tmp)
    assert "up" in calls and "--pull always" in calls and "--project-name blog" in calls
    rows = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute(
            "SELECT event, outcome, actor FROM audit_log"
            " WHERE event IN ('hook.create','hook.fire','project.action') ORDER BY id"
        )
        .fetchall()
    )
    assert [r[0] for r in rows] == ["hook.create", "project.action", "hook.fire"]
    assert "via key:hook-" in rows[2][2] and rows[2][1] == "success"


def test_the_token_is_shown_once_and_only_its_hash_is_kept(box):
    client, admin, node, tmp = box
    hook = new_hook(client, admin, node).json()
    listed = client.get("/api/v1/hooks", headers=admin).json()
    assert "token" not in json.dumps(listed) and listed["hooks"][0]["id"] == hook["id"]
    assert "project.redeploy" in listed["actions"]
    db = sqlite3.connect(tmp / "data" / "armadok.db")
    assert hook["token"] not in "".join(
        str(c) for row in db.execute("SELECT * FROM hooks") for c in row
    )


@pytest.mark.parametrize("token", [None, "", "wrong", "a" * 43])
def test_every_wrong_token_looks_the_same_and_is_counted(box, token):
    client, admin, node, _ = box
    hook = new_hook(client, admin, node).json()
    bad = fire(client, hook["id"], token)
    unknown = fire(client, "0" * 16, token or "x")
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json() == {"error": {"code": "hooks.invalid"}}


def test_wrong_tokens_are_slowed_down_but_the_right_one_is_never_locked_out(box):
    client, admin, node, _ = box
    hook = new_hook(client, admin, node).json()
    for _ in range(hook_routes.FAILURES_PER_MINUTE):
        assert fire(client, hook["id"], "wrong").status_code == 401
    slowed = fire(client, hook["id"], "wrong")
    assert slowed.status_code == 429 and slowed.json()["error"]["code"] == "hooks.too_many_attempts"
    # Someone guessing from the same address (a shared proxy, say) cannot stop the real caller.
    assert fire(client, hook["id"], hook["token"]).status_code == 202


def test_guessing_at_one_hook_from_many_addresses_is_also_slowed(box):
    client, admin, node, _ = box
    hook = new_hook(client, admin, node).json()
    for i in range(hook_routes.FAILURES_PER_HOOK):
        hook_routes.limiter.remember(ip=f"10.0.0.{i}", hook=hook["id"])
    assert fire(client, hook["id"], "wrong").status_code == 429
    assert fire(client, hook["id"], hook["token"]).status_code == 202


def test_refused_calls_to_a_real_hook_are_audited_once_a_minute_and_never_for_unknown_ones(box):
    client, admin, node, tmp = box
    hook = new_hook(client, admin, node).json()
    for _ in range(5):
        fire(client, hook["id"], "wrong")
        fire(client, "f" * 16, "wrong")
    rows = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute("SELECT object_id, details FROM audit_log WHERE event = 'hook.fire'")
        .fetchall()
    )
    assert [r[0] for r in rows] == [hook["id"]] and "hooks.invalid" in rows[0][1]


def test_a_call_refused_for_coming_too_soon_is_audited_and_does_not_postpone_the_next(
    box, monkeypatch
):
    client, admin, node, tmp = box
    monkeypatch.setattr(hook_routes, "MIN_INTERVAL_SECONDS", 60)
    hook = new_hook(client, admin, node).json()
    assert fire(client, hook["id"], hook["token"]).status_code == 202
    assert fire(client, hook["id"], hook["token"]).status_code == 429
    codes = [
        r[0]
        for r in sqlite3.connect(tmp / "data" / "armadok.db").execute(
            "SELECT details FROM audit_log WHERE event = 'hook.fire' AND outcome = 'failure'"
        )
    ]
    assert any("hooks.too_often" in c for c in codes)


def test_the_failure_memory_stays_bounded(box, monkeypatch):
    monkeypatch.setattr(hook_routes.limiter, "max_tracked", 50)
    for i in range(500):
        hook_routes.limiter.remember(ip=f"203.0.113.{i}", hook=f"{i:016x}")
    assert hook_routes.limiter.size() <= 120  # each kind keeps its own bounded table


def test_a_revoked_or_expired_hook_stops_working(box):
    client, admin, node, tmp = box
    hook = new_hook(client, admin, node).json()
    assert client.delete(f"/api/v1/hooks/{hook['id']}", headers=admin).status_code == 204
    assert fire(client, hook["id"], hook["token"]).status_code == 401
    soon = new_hook(client, admin, node, expires_at="2099-01-01T00:00:00+00:00").json()
    db = sqlite3.connect(tmp / "data" / "armadok.db")
    db.execute(
        "UPDATE hooks SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (soon["id"],)
    )
    db.commit()
    assert fire(client, soon["id"], soon["token"]).status_code == 401
    assert new_hook(client, admin, node, expires_at="2000-01-01T00:00:00+00:00").status_code == 422


def test_a_hook_cannot_be_called_too_often(box, monkeypatch):
    client, admin, node, _ = box
    monkeypatch.setattr(hook_routes, "MIN_INTERVAL_SECONDS", 60)
    hook = new_hook(client, admin, node).json()
    assert fire(client, hook["id"], hook["token"]).status_code == 202
    again = fire(client, hook["id"], hook["token"])
    assert again.status_code == 429 and again.json()["error"]["code"] == "hooks.too_often"


def test_a_hook_acts_only_as_far_as_its_creator_still_can(box):
    client, admin, node, tmp = box
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    for right in ("hooks.manage", "nodes.read", "projects.read", "projects.deploy"):
        scope = "*" if right == "hooks.manage" else f"node:{node}"
        client.post(
            f"/api/v1/users/{bob_id}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    bob = login(client, "bob")
    hook = new_hook(client, bob, node).json()
    # The creator loses the deploy right: the hook stops with it, and says why.
    grants = client.get(f"/api/v1/users/{bob_id}/grants", headers=admin).json()
    gone = next(g["id"] for g in grants if g["right"] == "projects.deploy")
    client.delete(f"/api/v1/users/{bob_id}/grants/{gone}", headers=admin)
    refused = fire(client, hook["id"], hook["token"])
    assert (
        refused.status_code == 403 and refused.json()["error"]["code"] == "hooks.creator_lost_right"
    )
    assert not (tmp / "calls").exists()
    # A disabled creator: same.
    client.patch(f"/api/v1/users/{bob_id}", headers=admin, json={"is_active": False})
    assert fire(client, hook["id"], hook["token"]).status_code == 403


def test_nobody_builds_a_hook_beyond_their_own_rights(box):
    client, admin, node, _ = box
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "hooks.manage", "scope": "*"},
    )
    bob = login(client, "bob")
    assert new_hook(client, bob, node).status_code == 404  # cannot even see the machine
    for right in ("nodes.read", "projects.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    assert new_hook(client, bob, node).status_code == 403  # sees it, may not deploy there
    carol = client.post(
        "/api/v1/users", headers=admin, json={"username": "carol", "password": PASSWORD}
    )
    assert carol.status_code == 201
    assert (
        new_hook(client, login(client, "carol"), node).status_code == 403
    )  # no hooks.manage at all


@pytest.mark.parametrize(
    "body",
    [
        {"action": "project.redeploy", "target": {"node_id": "NODE", "project": "../x"}},
        {
            "action": "project.redeploy",
            "target": {"node_id": "NODE", "project": "blog", "extra": "1"},
        },
        {"action": "project.redeploy", "target": {"project": "blog"}},
        {"action": "rm -rf", "target": {"node_id": "NODE", "project": "blog"}},
        {"action": "project.redeploy", "target": {"node_id": "bad id", "project": "blog"}},
    ],
)
def test_bad_hooks_are_refused(box, body):
    client, admin, node, _ = box
    body = json.loads(json.dumps(body).replace("NODE", node))
    r = client.post("/api/v1/hooks", headers=admin, json={"name": "x", **body})
    assert r.status_code in (404, 422)
    assert client.get("/api/v1/hooks", headers=admin).json()["hooks"] == []


def test_only_your_own_hooks_can_be_listed_or_revoked_and_keys_cannot_manage_hooks(box):
    client, admin, node, _ = box
    hook = new_hook(client, admin, node).json()
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "hooks.manage", "scope": "*"},
    )
    bob = login(client, "bob")
    assert client.get("/api/v1/hooks", headers=bob).json()["hooks"] == []
    assert client.delete(f"/api/v1/hooks/{hook['id']}", headers=bob).status_code == 404
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "ci", "rights": ["hooks.manage"]}
    )
    assert key.status_code == 422  # sensitive: never carried by a key


# --- metrics --------------------------------------------------------------------------------


def test_metrics_need_their_own_right_and_show_only_what_the_key_owner_sees(box):
    client, admin, node, _ = box
    assert client.get("/api/v1/metrics").status_code == 401
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob = login(client, "bob")
    assert client.get("/api/v1/metrics", headers=bob).status_code == 403
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "metrics.read", "scope": "*"},
    )
    empty = client.get("/api/v1/metrics", headers=bob)
    assert empty.status_code == 200 and empty.headers["content-type"].startswith(
        "text/plain; version=0.0.4"
    )
    assert "armadok_worker_connected{" not in empty.text  # sees no machine, so none is listed
    full = client.get("/api/v1/metrics", headers=admin)
    assert full.status_code == 200 and f'node_id="{node}"' in full.text


def test_metrics_for_the_owner_of_a_machine(box):
    client, admin, node, _ = box
    page = client.get("/api/v1/metrics", headers=admin)
    assert page.status_code == 200
    text = page.text
    assert "# TYPE armadok_nodes gauge" in text and 'armadok_nodes{type="full"} 1' in text
    assert re.search(rf'armadok_worker_connected\{{node="[^"]*",node_id="{node}"\}} [01]', text)
    assert "armadok_info{version=" in text and "armadok_live_connections" in text
    # A task shows up in the counts.
    h = new_hook(client, admin, node).json()
    fire(client, h["id"], h["token"])
    time.sleep(0.5)
    assert "armadok_deploy_tasks{state=" in client.get("/api/v1/metrics", headers=admin).text


def test_metrics_labels_are_escaped(box):
    from armadok.modules.metrics.routes import _label

    assert _label('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_a_metrics_key_works_as_a_bearer_token(box):
    client, admin, node, _ = box
    key = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={"name": "prom", "rights": ["metrics.read"], "read_only": True},
    )
    assert key.status_code == 201, key.text
    got = client.get("/api/v1/metrics", headers={"Authorization": f"Bearer {key.json()['key']}"})
    assert got.status_code == 200 and "armadok_info" in got.text


def test_a_hook_does_not_wake_up_when_its_creator_is_enabled_again(box):
    client, admin, node, _ = box
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    for right, scope in (
        ("hooks.manage", "*"),
        ("nodes.read", f"node:{node}"),
        ("projects.read", f"node:{node}"),
        ("projects.deploy", f"node:{node}"),
    ):
        client.post(
            f"/api/v1/users/{bob_id}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    hook = new_hook(client, login(client, "bob"), node).json()
    client.patch(f"/api/v1/users/{bob_id}", headers=admin, json={"is_active": False})
    client.patch(f"/api/v1/users/{bob_id}", headers=admin, json={"is_active": True})
    refused = fire(client, hook["id"], hook["token"])
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "hooks.unavailable"
    # A hook made after the account came back works.
    fresh = new_hook(client, login(client, "bob"), node).json()
    assert fire(client, fresh["id"], fresh["token"]).status_code == 202


def test_metrics_are_also_served_at_the_root_and_never_cached(box):
    client, admin, node, _ = box
    assert client.get("/metrics").status_code == 401
    page = client.get("/metrics", headers=admin)
    assert page.status_code == 200 and f'node_id="{node}"' in page.text
    assert page.headers["cache-control"] == "no-store"
    slash = client.get("/metrics/", headers=admin, follow_redirects=False)
    assert slash.headers["cache-control"] == "no-store"
    assert "/metrics" not in client.get("/api/openapi.json").json()["paths"]
