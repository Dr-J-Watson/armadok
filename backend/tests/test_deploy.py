import json
import logging
import os
import re
import stat
import time

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app

PASSWORD = "correct horse battery"
MODULES = (
    "users",
    "apikeys",
    "nodes",
    "workers",
    "docker",
    "projects",
    "relations",
    "audit",
    "notifications",
)
FAKE = """#!/bin/sh
DIR=$(dirname "$0")
echo "$@" >> "$DIR/calls"
case "$(cat "$DIR/mode")" in
  ok) echo "Container blog-web-1 Started"; exit 0 ;;
  fail) echo "build error: no such image"; exit 1 ;;
  slow) sleep 30 ;;
  ps) printf '%s\\n' "$(cat "$DIR/ps.out")" ;;
  logs) echo "web | listening on 80"; exit 0 ;;
esac
"""


@pytest.fixture
def box(tmp_path, monkeypatch, caplog):
    stacks = tmp_path / "stacks"
    (stacks / "blog").mkdir(parents=True)
    (stacks / "blog" / "compose.yaml").write_text("services: {}\n")
    fake = tmp_path / "docker"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "mode").write_text("ok")
    service = {
        "Service": "web",
        "Name": "blog-web-1",
        "State": "running",
        "Status": "Up",
        "Image": "nginx:1",
    }
    (tmp_path / "ps.out").write_text(json.dumps(service))
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(stacks))
    monkeypatch.setenv("ARMADOK_DOCKER_BIN", str(fake))
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", str(tmp_path / "none.sock"))
    from armadok.modules.projects import deploy
    from armadok.worker import compose_ops

    monkeypatch.setattr(deploy, "POLL_SECONDS", 0.05)
    compose_ops._tasks.clear()
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
    yield client, app, admin, node, tmp_path
    compose_ops._tasks.clear()


def mode(tmp_path, value):
    (tmp_path / "mode").write_text(value)


def wait_done(client, admin, node, task_id, seconds=10):
    for _ in range(int(seconds * 20)):
        got = client.get(f"/api/v1/nodes/{node}/tasks/{task_id}", headers=admin).json()
        if got["state"] != "running":
            return got
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def start(client, admin, node, **body):
    return client.post(
        f"/api/v1/nodes/{node}/projects/blog/actions", headers=admin, json={"action": "up", **body}
    )


def test_up_runs_in_the_background_and_the_end_is_recorded_audited_and_announced(box):
    import sqlite3

    client, app, admin, node, tmp = box
    started = start(client, admin, node, build=True)
    assert started.status_code == 202 and started.json()["state"] == "running"
    done = wait_done(client, admin, node, started.json()["task_id"])
    assert done["state"] == "success" and done["exit_code"] == 0 and "Started" in done["output"]
    assert "--build" in (tmp / "calls").read_text()
    time.sleep(0.2)
    rows = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute(
            "SELECT event, outcome FROM audit_log WHERE event LIKE 'project.action%' ORDER BY id"
        )
        .fetchall()
    )
    assert rows == [("project.action", "success"), ("project.action_done", "success")]
    titles = [
        e["title"] for e in client.get("/api/v1/notifications", headers=admin).json()["entries"]
    ]
    assert titles == ["Deployment succeeded"]  # once, however many times the task is read
    client.get(f"/api/v1/nodes/{node}/tasks/{started.json()['task_id']}", headers=admin)
    assert len(client.get("/api/v1/notifications", headers=admin).json()["entries"]) == 1


def test_a_failed_deployment_says_so_with_its_output(box):
    client, app, admin, node, tmp = box
    mode(tmp, "fail")
    task = start(client, admin, node).json()["task_id"]
    done = wait_done(client, admin, node, task)
    assert (
        done["state"] == "failed" and done["exit_code"] == 1 and "no such image" in done["output"]
    )
    time.sleep(0.2)
    titles = [
        e["title"] for e in client.get("/api/v1/notifications", headers=admin).json()["entries"]
    ]
    assert titles == ["Deployment failed"]


def test_output_is_stored_encrypted_and_the_history_lists_tasks_without_it(box):
    import sqlite3

    client, app, admin, node, tmp = box
    wait_done(client, admin, node, start(client, admin, node).json()["task_id"])
    raw = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute("SELECT output, encrypted FROM deploy_tasks")
        .fetchone()
    )
    assert raw[1] == 1 and "Started" not in raw[0]
    listed = client.get(f"/api/v1/nodes/{node}/tasks?project=blog", headers=admin).json()
    assert len(listed) == 1 and "output" not in listed[0] and listed[0]["project"] == "blog"


def test_only_one_action_at_a_time_and_bad_requests_are_refused(box):
    client, app, admin, node, tmp = box
    mode(tmp, "slow")
    first = start(client, admin, node)
    assert first.status_code == 202
    second = client.post(
        f"/api/v1/nodes/{node}/projects/blog/actions", headers=admin, json={"action": "down"}
    )
    assert second.status_code == 409 and second.json()["error"]["code"] == "worker.project_busy"
    base = f"/api/v1/nodes/{node}/projects/blog/actions"
    for bad in (
        {"action": "rm -rf /"},
        {"action": "up", "files": ["../../etc/passwd"]},
        {"action": "restart", "service": "web; id"},
        {"action": "up", "files": []},
        {},
    ):
        assert client.post(base, headers=admin, json=bad).status_code == 422, bad
    assert client.post(
        f"/api/v1/nodes/{node}/projects/..%2fx/actions", headers=admin, json={"action": "up"}
    ).status_code in (404, 422)


def test_deploying_needs_its_own_sensitive_right_that_no_api_key_can_carry(box):
    client, app, admin, node, tmp = box
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}

    def grant(right):
        body = {"right": right, "scope": f"node:{node}"}
        return client.post(f"/api/v1/users/{bob_id}/grants", headers=admin, json=body)

    assert start(client, bob, node).status_code == 404  # cannot even see the node
    for right in ("nodes.read", "projects.read", "projects.files.write"):
        grant(right)
    refused = start(client, bob, node)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "rights.missing"
    assert (
        client.get(f"/api/v1/nodes/{node}/projects/blog/status", headers=bob).status_code == 200
    )  # read is enough
    assert client.get(f"/api/v1/nodes/{node}/projects/blog/logs", headers=bob).status_code == 403
    grant("projects.deploy")
    task = start(client, bob, node).json()["task_id"]
    assert (
        client.get(f"/api/v1/nodes/{node}/tasks/{task}", headers=bob).status_code == 403
    )  # logs are a separate right
    # An administrator's key cannot be given the deploy right at all.
    key = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={"name": "ci", "rights": ["projects.deploy", "projects.read", "nodes.read"]},
    )
    assert key.status_code == 422 and key.json()["error"]["code"] == "apikeys.invalid_right"


def test_status_logs_check_and_audited_log_reads(box):
    import sqlite3

    client, app, admin, node, tmp = box
    mode(tmp, "ps")
    status = client.get(f"/api/v1/nodes/{node}/projects/blog/status", headers=admin).json()
    assert status["services"][0]["service"] == "web" and status["services"][0]["state"] == "running"
    mode(tmp, "logs")
    logs = client.get(
        f"/api/v1/nodes/{node}/projects/blog/logs?service=web&tail=50", headers=admin
    ).json()
    assert "listening on 80" in logs["output"] and "--tail 50 web" in (tmp / "calls").read_text()
    assert (
        client.get(f"/api/v1/nodes/{node}/projects/blog/logs?tail=99999", headers=admin).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/nodes/{node}/projects/blog/logs?service=a;b", headers=admin
        ).status_code
        == 422
    )
    mode(tmp, "ok")
    assert client.post(f"/api/v1/nodes/{node}/projects/blog/check", headers=admin).json() == {
        "valid": True,
        "errors": "",
    }
    rows = (
        sqlite3.connect(tmp / "data" / "armadok.db")
        .execute("SELECT details FROM audit_log WHERE event = 'project.logs_read'")
        .fetchall()
    )
    assert (
        len(rows) == 1 and "listening" not in rows[0][0]
    )  # that logs were read is recorded, not what they said


def test_the_hubs_secrets_never_reach_the_command(box):
    client, app, admin, node, tmp = box
    (tmp / "docker").write_text(
        FAKE.replace('echo "$@" >> "$DIR/calls"', 'echo "$@" >> "$DIR/calls"; env > "$DIR/env"')
    )
    wait_done(client, admin, node, start(client, admin, node).json()["task_id"])
    seen = (tmp / "env").read_text()
    assert (
        "ARMADOK_ENCRYPTION_KEY" not in seen
        and "ARMADOK_PROJECTS_DIR" not in seen
        and "DOCKER_HOST=" in seen
    )


def test_a_task_is_only_visible_through_its_own_node(box):
    client, app, admin, node, tmp = box
    task = start(client, admin, node).json()["task_id"]
    other = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "other", "type": "docker"}
    ).json()["id"]
    assert client.get(f"/api/v1/nodes/{other}/tasks/{task}", headers=admin).status_code == 404
    assert os.path.exists(tmp / "docker")


def test_a_linked_compose_file_is_refused_and_check_needs_the_logs_right(box):
    client, app, admin, node, tmp = box
    stacks = tmp / "stacks" / "blog"
    (stacks / ".env").symlink_to(tmp / "data")
    check = client.post(f"/api/v1/nodes/{node}/projects/blog/check", headers=admin)
    assert check.status_code == 422 and check.json()["error"]["code"] == "worker.invalid_path"
    assert not (tmp / "calls").exists()  # nothing was run
    (stacks / ".env").unlink()
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    for right in ("nodes.read", "projects.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    denied = client.post(
        f"/api/v1/nodes/{node}/projects/blog/check", headers={"Authorization": f"Bearer {token}"}
    )
    assert denied.status_code == 403


def test_without_an_encryption_key_the_output_is_not_stored(box):
    import dataclasses
    import sqlite3

    from armadok.modules.projects import tasks

    client, app, admin, node, tmp = box
    conn = sqlite3.connect(tmp / "data" / "armadok.db")
    conn.row_factory = sqlite3.Row
    task = tasks.create(conn, node, "w1", "blog", "up", "someone")
    keyless = dataclasses.replace(app.state.settings, encryption_key=None)
    assert tasks.finish(conn, keyless, task, "success", 0, "token=abc", False)
    assert conn.execute("SELECT output FROM deploy_tasks WHERE id = ?", (task,)).fetchone()[0] == ""


def test_running_tasks_are_followed_again_after_a_restart(box):
    import asyncio

    from armadok.modules.projects import deploy

    client, app, admin, node, tmp = box
    mode(tmp, "slow")
    task = start(client, admin, node).json()["task_id"]
    followed = []
    original = deploy._spawn_watcher
    deploy._spawn_watcher = lambda a, n, t: followed.append((n, t))
    try:
        asyncio.run(deploy.resume_watchers(app))
    finally:
        deploy._spawn_watcher = original
    assert followed == [(node, task)]
