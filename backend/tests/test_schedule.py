import asyncio
import json
import logging
import re
import socketserver
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.schedule import repository as repo
from armadok.modules.schedule import routes as schedule_routes
from armadok.modules.schedule import runner, timing
from armadok.worker import docker_control
from armadok.worker.operation import OperationError

PASSWORD = "correct horse battery"
SEEN: list[tuple[str, str]] = []


NETWORKS: list[dict] = []
CONTAINERS: list[dict] = []
DELETED: list[str] = []


class Daemon(BaseHTTPRequestHandler):
    """A stand-in for the Docker daemon: answers the prune requests and the network listings."""

    def _send(self, status, body):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        SEEN.append((self.command, self.path))
        path = urlsplit(self.path).path
        self._send(200, {"/networks": NETWORKS, "/containers/json": CONTAINERS}.get(path, []))

    def do_DELETE(self):
        SEEN.append((self.command, self.path))
        DELETED.append(self.path.rsplit("/", 1)[-1])
        self._send(204, None)

    def do_POST(self):
        SEEN.append((self.command, self.path))
        path = urlsplit(self.path).path
        body = {
            "/containers/prune": {"ContainersDeleted": ["a", "b"], "SpaceReclaimed": 10},
            "/images/prune": {"ImagesDeleted": [{"Deleted": "x"}], "SpaceReclaimed": 5},
            "/networks/prune": {"NetworksDeleted": None},
        }.get(path)
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    SEEN.clear()
    NETWORKS.clear()
    CONTAINERS.clear()
    DELETED.clear()
    path = str(tmp_path / "d.sock")
    server = Server(path, Daemon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", path)
    yield
    server.shutdown()


# --- timing -----------------------------------------------------------------------------------


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_an_interval_runs_every_n_minutes():
    spec = timing.validate({"kind": "interval", "every_minutes": 90})
    assert timing.next_run(spec, at("2026-01-01T10:00:00")) == at("2026-01-01T11:30:00")


def test_a_daily_task_runs_at_the_local_time_of_its_zone():
    spec = timing.validate({"kind": "daily", "at": "03:30", "timezone": "Europe/Paris"})
    # January in Paris is UTC+1: 03:30 local is 02:30 UTC; the next one after 02:30 is tomorrow's.
    assert timing.next_run(spec, at("2026-01-10T00:00:00")) == at("2026-01-10T02:30:00")
    assert timing.next_run(spec, at("2026-01-10T02:30:00")) == at("2026-01-11T02:30:00")
    # In July Paris is UTC+2.
    assert timing.next_run(spec, at("2026-07-10T00:00:00")) == at("2026-07-10T01:30:00")


def test_weekdays_are_respected():
    spec = timing.validate({"kind": "daily", "at": "04:00", "weekdays": [0]})  # Mondays
    assert at("2026-01-05T04:00:00").weekday() == 0
    assert timing.next_run(spec, at("2026-01-05T04:00:00")) == at("2026-01-12T04:00:00")
    assert timing.next_run(spec, at("2026-01-06T00:00:00")) == at("2026-01-12T04:00:00")


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "interval", "every_minutes": 5},
        {"kind": "interval", "every_minutes": 10**9},
        {"kind": "interval", "every_minutes": True},
        {"kind": "daily", "at": "25:00"},
        {"kind": "daily", "at": "3:30"},
        {"kind": "daily", "at": "03:30", "timezone": "Mars/Base"},
        {"kind": "daily", "at": "03:30", "timezone": "../../etc/passwd"},
        {"kind": "daily", "at": "03:30", "weekdays": [7]},
        {"kind": "daily", "at": "03:30", "weekdays": "all"},
        {"kind": "cron", "expr": "* * * * *"},
        {},
    ],
)
def test_nonsense_schedules_are_refused(spec):
    with pytest.raises(ValueError):
        timing.validate(spec)


# --- the worker's prune -----------------------------------------------------------------------


def test_prune_sends_one_fixed_request_with_protection_filters(daemon):
    answer = docker_control.prune({"kind": "containers"})
    assert answer == {"removed": 2, "space": 10}
    ((method, path),) = SEEN
    assert method == "POST" and path.startswith("/containers/prune?filters=")
    filters = json.loads(parse_qs(urlsplit(path).query)["filters"][0])
    assert filters == {"label!": ["armadok.protect=true"]}


def test_only_dangling_images_are_pruned_and_volumes_never(daemon):
    assert docker_control.prune({"kind": "images"}) == {"removed": 1, "space": 5}
    filters = json.loads(parse_qs(urlsplit(SEEN[0][1]).query)["filters"][0])
    assert filters == {"dangling": ["true"]}
    with pytest.raises(OperationError):
        docker_control.prune({"kind": "volumes"})
    assert all("volumes" not in path for _, path in SEEN)


@pytest.mark.parametrize("kind", [None, "", "all", ["images"], 5, "Images"])
def test_unknown_prune_kinds_never_reach_the_daemon(daemon, kind):
    with pytest.raises(OperationError) as error:
        docker_control.prune({"kind": kind})
    assert error.value.code == "worker.invalid_path" and SEEN == []


# --- the API ----------------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(schedule_routes, "MIN_RUN_GAP_SECONDS", 0)
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
        yield client, admin, node, app


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def person(client, admin, name, grants=()):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    ids = []
    for right, scope in grants:
        ids.append(
            client.post(
                f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
            ).json()["id"]
        )
    return uid, login(client, name), ids


def make(client, who, node, **extra):
    body = {
        "name": "weekly prune",
        "action": "docker.prune",
        "target": {"kind": "images"},
        "schedule": {"kind": "interval", "every_minutes": 60},
        **extra,
    }
    return client.post(f"/api/v1/nodes/{node}/schedule", headers=who, json=body)


def row(app, task_id):
    conn = app.state.settings.open_db()
    try:
        return repo.get(conn, task_id)
    finally:
        conn.close()


def test_rights_and_visibility(box):
    client, admin, node, _ = box
    scope = f"node:{node}"
    _, reader, _ = person(
        client, admin, "reader", [("nodes.read", scope), ("schedule.read", scope)]
    )
    _, stranger, _ = person(client, admin, "stranger")
    assert make(client, admin, node).status_code == 201
    listed = client.get(f"/api/v1/nodes/{node}/schedule", headers=reader)
    assert listed.status_code == 200 and listed.json()["can_manage"] is False
    assert make(client, reader, node).status_code == 403
    assert client.get(f"/api/v1/nodes/{node}/schedule", headers=stranger).status_code == 404
    assert make(client, stranger, node).status_code == 404


def test_scheduling_needs_the_right_of_the_action_itself(box):
    client, admin, node, _ = box
    scope = f"node:{node}"
    rights = [("nodes.read", scope), ("schedule.read", scope), ("schedule.manage", scope)]
    _, bob, _ = person(client, admin, "bob", rights)
    # Bob may schedule, but he could not prune by hand: he may not schedule a prune either.
    assert make(client, bob, node).status_code == 403
    # And the form only offers him what he can do.
    assert client.get(f"/api/v1/nodes/{node}/schedule", headers=bob).json()["actions"] == []
    names = [
        a["name"]
        for a in client.get(f"/api/v1/nodes/{node}/schedule", headers=admin).json()["actions"]
    ]
    assert "docker.prune" in names and "project.restart" in names


def test_a_key_cannot_manage_schedules(box):
    client, admin, node, _ = box
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["schedule.manage"]}
    )
    assert key.status_code == 422 and key.json()["error"]["code"] == "apikeys.invalid_right"


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"action": "nothing.here"}, "schedule.unknown_action"),
        ({"target": {"kind": "volumes"}}, "schedule.invalid_target"),
        ({"target": {"kind": "images", "node_id": "other"}}, "schedule.invalid_target"),
        ({"target": {}}, "schedule.invalid_target"),
        ({"schedule": {"kind": "interval", "every_minutes": 1}}, "schedule.invalid_schedule"),
        ({"name": "   "}, "schedule.invalid_name"),
    ],
)
def test_invalid_tasks_are_refused_with_a_code(box, extra, code):
    client, admin, node, _ = box
    got = make(client, admin, node, **extra)
    assert got.status_code == 422 and got.json()["error"]["code"] == code


def test_incoming_hooks_cannot_use_schedule_only_actions(box):
    client, admin, node, _ = box
    got = client.post(
        "/api/v1/hooks",
        headers=admin,
        json={"name": "h", "action": "docker.prune", "target": {"node_id": node, "kind": "images"}},
    )
    assert got.status_code == 422 and got.json()["error"]["code"] == "hooks.unknown_action"
    assert "docker.prune" not in client.get("/api/v1/hooks", headers=admin).json()["actions"]
    assert "project.redeploy" in client.get("/api/v1/hooks", headers=admin).json()["actions"]


def test_the_number_of_tasks_is_capped(box, monkeypatch):
    client, admin, node, _ = box
    monkeypatch.setattr(schedule_routes, "MAX_PER_NODE", 1)
    assert make(client, admin, node).status_code == 201
    assert make(client, admin, node).json()["error"]["code"] == "schedule.too_many"


def test_change_pause_and_delete(box):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    url = f"/api/v1/nodes/{node}/schedule/{tid}"
    before = row(app, tid)["next_run"]
    got = client.patch(
        url,
        headers=admin,
        json={"enabled": False, "schedule": {"kind": "interval", "every_minutes": 120}},
    )
    assert got.json()["enabled"] is False and got.json()["next_run"] is None
    assert row(app, tid)["next_run"] != before
    assert client.patch(url, headers=admin, json={"action": "x"}).status_code == 422
    assert client.patch(url, headers=admin, json={"schedule": {"kind": "x"}}).status_code == 422
    assert client.delete(url, headers=admin).status_code == 204
    assert client.delete(url, headers=admin).status_code == 404
    assert client.delete(f"/api/v1/nodes/other/schedule/{tid}", headers=admin).status_code == 404


def test_running_now_prunes_and_is_audited_as_the_schedule(box, daemon):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    got = client.post(f"/api/v1/nodes/{node}/schedule/{tid}/run", headers=admin)
    assert got.json() == {"result": "ok"}
    assert [m for m, p in SEEN] == ["POST"] and SEEN[0][1].startswith("/images/prune")
    assert row(app, tid)["last_result"] == "ok"
    log = client.get("/api/v1/audit", headers=admin).text
    assert "schedule.run" in log and f"via schedule:{tid}" in log


def test_a_task_stops_working_when_its_creator_loses_the_right(box, daemon):
    client, admin, node, app = box
    scope = f"node:{node}"
    rights = [
        ("nodes.read", scope),
        ("schedule.read", scope),
        ("schedule.manage", scope),
        ("docker.read", scope),
        ("docker.control", scope),
        ("docker.remove", scope),
    ]
    uid, bob, ids = person(client, admin, "bob", rights)
    tid = make(client, bob, node).json()["id"]
    assert client.post(f"/api/v1/nodes/{node}/schedule/{tid}/run", headers=bob).json() == {
        "result": "ok"
    }
    # The right goes away: the task does nothing more with it (and the daemon is not touched).
    SEEN.clear()
    client.delete(f"/api/v1/users/{uid}/grants/{ids[-1]}", headers=admin)
    got = client.post(f"/api/v1/nodes/{node}/schedule/{tid}/run", headers=admin)
    assert got.json() == {"result": "schedule.creator_lost_right"} and SEEN == []


def test_a_task_of_a_disabled_account_does_not_run(box, daemon):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    conn = app.state.settings.open_db()
    conn.execute(
        "UPDATE users SET is_active = 0, deactivated_at = ? WHERE username = 'admin'",
        ((datetime.now(UTC) + timedelta(seconds=5)).isoformat(),),
    )
    conn.commit()
    conn.close()
    result = client.portal.call(runner.run_task, app, row(app, tid))
    assert result == "schedule.creator_unavailable" and SEEN == []


def test_failures_are_reported_once_with_their_code(box, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(runner, "emit", fake_emit)
    tid = make(client, admin, node).json()["id"]
    # No daemon is listening: the prune fails and says so.
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", "/nonexistent/docker.sock")
    result = client.portal.call(runner.run_task, app, row(app, tid))
    assert result.startswith("worker.") and sent[0]["event"] == "schedule.failed"
    assert sent[0]["params"]["code"] == result and "/nonexistent" not in str(sent)


def test_due_tasks_run_once_and_late_ones_are_skipped(box, daemon):
    client, admin, node, app = box
    on_time = make(client, admin, node, name="on time").json()["id"]
    late = make(client, admin, node, name="late").json()["id"]
    off = make(client, admin, node, name="off").json()["id"]
    future = make(client, admin, node, name="future").json()["id"]
    conn = app.state.settings.open_db()
    now = datetime.now(UTC)
    for task, when in (
        (on_time, now - timedelta(minutes=1)),
        (late, now - timedelta(hours=3)),
        (off, now - timedelta(minutes=1)),
    ):
        conn.execute(
            "UPDATE scheduled_tasks SET next_run = ? WHERE id = ?", (when.isoformat(), task)
        )
    conn.execute("UPDATE scheduled_tasks SET enabled = 0 WHERE id = ?", (off,))
    conn.commit()
    conn.close()
    assert client.portal.call(runner.run_due, app) == 2
    assert row(app, on_time)["last_result"] == "ok"
    assert row(app, late)["last_result"] == "skipped"
    assert row(app, off)["last_result"] is None and row(app, future)["last_result"] is None
    assert len(SEEN) == 1
    # The slot is used up: the next round finds nothing to do.
    assert client.portal.call(runner.run_due, app) == 0
    assert datetime.fromisoformat(row(app, on_time)["next_run"]) > now


def test_a_task_does_not_run_twice_at_the_same_time(box, daemon):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    runner._running.add(tid)
    try:
        assert client.portal.call(runner.run_task, app, row(app, tid)) == "schedule.already_running"
    finally:
        runner._running.discard(tid)
    assert SEEN == []


def test_a_manager_without_the_action_right_cannot_run_retime_or_delete_it(box, daemon):
    client, admin, node, app = box
    scope = f"node:{node}"
    rights = [("nodes.read", scope), ("schedule.read", scope), ("schedule.manage", scope)]
    _, bob, _ = person(client, admin, "bob", rights)
    tid = make(client, admin, node).json()["id"]
    url = f"/api/v1/nodes/{node}/schedule/{tid}"
    assert client.post(url + "/run", headers=bob).status_code == 403
    assert client.patch(url, headers=bob, json={"enabled": False}).status_code == 403
    assert client.delete(url, headers=bob).status_code == 403
    assert SEEN == []
    # He may see that a task exists, but not what it targets.
    shown = client.get(f"/api/v1/nodes/{node}/schedule", headers=bob).json()["tasks"][0]
    assert shown["target"] == {} and shown["created_by"] is None


def test_the_caller_is_named_in_the_audit_entry_of_a_manual_run(box, daemon):
    client, admin, node, _ = box
    tid = make(client, admin, node).json()["id"]
    client.post(f"/api/v1/nodes/{node}/schedule/{tid}/run", headers=admin)
    assert "requested_by" in client.get("/api/v1/audit", headers=admin).text


def test_renaming_does_not_postpone_a_task_but_retiming_and_resuming_do(box):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    url = f"/api/v1/nodes/{node}/schedule/{tid}"
    before = row(app, tid)["next_run"]
    time.sleep(0.01)
    client.patch(url, headers=admin, json={"name": "renamed"})
    client.patch(url, headers=admin, json={"enabled": True})
    assert row(app, tid)["next_run"] == before
    client.patch(url, headers=admin, json={"enabled": False})
    time.sleep(0.01)
    client.patch(url, headers=admin, json={"enabled": True})
    assert row(app, tid)["next_run"] != before


def test_a_slot_can_be_taken_only_once(box):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    conn = app.state.settings.open_db()
    try:
        task = repo.get(conn, tid)
        assert repo.advance(conn, task) is True
        assert repo.advance(conn, task) is False  # the same slot, already taken
    finally:
        conn.close()


def test_a_missed_slot_is_audited_and_does_not_look_like_a_run(box, daemon):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    conn = app.state.settings.open_db()
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    conn.execute("UPDATE scheduled_tasks SET next_run = ? WHERE id = ?", (old, tid))
    conn.commit()
    conn.close()
    client.portal.call(runner.run_due, app)
    assert row(app, tid)["last_result"] == "skipped" and row(app, tid)["last_run"] is None
    assert "schedule.skipped_late" in client.get("/api/v1/audit", headers=admin).text and SEEN == []


def test_a_stuck_run_is_cut_off(box, monkeypatch):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    monkeypatch.setattr(runner, "RUN_TIMEOUT_SECONDS", 0.2)

    async def stuck(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(runner, "_run", stuck)
    assert client.portal.call(runner.run_task, app, row(app, tid)) == "schedule.timeout"
    assert tid not in runner._running


# --- cron and missed runs ---------------------------------------------------------------------


def test_cron_runs_at_the_listed_times_in_the_zone():
    spec = timing.validate({"kind": "cron", "expr": "30 3 * * 1-5", "timezone": "Europe/Paris"})
    # Friday 2026-01-09 at 03:30 Paris is 02:30 UTC; the next one skips the weekend.
    assert timing.next_run(spec, at("2026-01-09T02:00:00")) == at("2026-01-09T02:30:00")
    assert timing.next_run(spec, at("2026-01-09T02:30:00")) == at("2026-01-12T02:30:00")


def test_cron_supports_steps_lists_names_and_sunday_as_zero_or_seven():
    spec = timing.validate({"kind": "cron", "expr": "0 */6 * jan,feb sun"})
    assert timing.next_run(spec, at("2026-01-01T00:00:00")) == at("2026-01-04T00:00:00")
    seven = timing.validate({"kind": "cron", "expr": "0 12 * * 7"})
    zero = timing.validate({"kind": "cron", "expr": "0 12 * * 0"})
    assert timing.next_run(seven, at("2026-01-01T00:00:00")) == at("2026-01-04T12:00:00")
    assert timing.next_run(zero, at("2026-01-01T00:00:00")) == at("2026-01-04T12:00:00")


def test_cron_day_of_month_or_day_of_week_when_both_are_set():
    spec = timing.validate({"kind": "cron", "expr": "0 8 13 * fri"})
    # Friday 2026-02-13 matches both; 2026-01-02 is a Friday, not the 13th.
    assert timing.next_run(spec, at("2026-01-01T00:00:00")) == at("2026-01-02T08:00:00")


@pytest.mark.parametrize(
    "expr",
    [
        "* * * * *",  # every minute: too often
        "0,5 * * * *",  # five minutes apart: too often
        "*/10 * * * *",
        "0 0 31 2 *",  # no such date
        "0 25 * * *",
        "0 0 * *",
        "0 0 * * * *",
        "a b c d e",
        "0 0 */0 * *",
        "$(reboot) * * * *",
        "0 0 " + "1," * 80 + "1 * *",
    ],
)
def test_bad_or_too_frequent_cron_expressions_are_refused(expr):
    with pytest.raises(ValueError):
        timing.validate({"kind": "cron", "expr": expr})


def test_a_missed_run_waits_for_its_manager_then_is_replaced_by_a_notice(box, daemon, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(runner, "emit", fake_emit)
    tid = make(client, admin, node).json()["id"]

    def set_next(when):
        conn = app.state.settings.open_db()
        conn.execute("UPDATE scheduled_tasks SET next_run = ? WHERE id = ?", (when, tid))
        conn.commit()
        conn.close()

    late = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    set_next(late)
    client.portal.call(runner.run_due, app)
    assert row(app, tid)["missed_slot"] == late and SEEN == []
    assert [m["event"] for m in sent] == ["schedule.missed"]
    assert sent[0]["user_ids"] and sent[0]["params"]["name"] == "weekly prune"
    # Nobody answers; at the next time the question becomes a notice.
    set_next((datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    client.portal.call(runner.run_due, app)
    assert [m["event"] for m in sent][-1] == "schedule.missed_final"
    assert row(app, tid)["missed_slot"] is None and len(SEEN) == 1


def test_the_machine_owner_is_told_too_when_the_manager_cannot_answer(box, daemon, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(runner, "emit", fake_emit)
    tid = make(client, admin, node).json()["id"]
    conn = app.state.settings.open_db()
    task = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (tid,)).fetchone()
    now = datetime.now(UTC)
    # A manager who is also the owner has nobody to be replaced by.
    assert runner._stand_in(conn, task, now) is None
    conn.execute("UPDATE nodes SET owner = 'owner-id' WHERE id = ?", (node,))
    # An active manager who did something today still answers for themselves.
    assert runner._stand_in(conn, task, now) is None
    # A month later with no activity, or once disabled, the owner takes over.
    assert runner._stand_in(conn, task, now + timedelta(days=31)) == "owner-id"
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (task["created_by"],))
    assert runner._stand_in(conn, task, now) == "owner-id"
    conn.close()


def test_the_manager_can_run_a_missed_task_or_wait_and_only_the_first_answer_counts(box, daemon):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    url = f"/api/v1/nodes/{node}/schedule/{tid}/missed"
    assert client.post(url, headers=admin, json={"decision": "run"}).status_code == 409
    conn = app.state.settings.open_db()
    repo.set_skipped(conn, tid, "2026-01-01T00:00:00+00:00")
    conn.close()
    assert client.post(url, headers=admin, json={"decision": "later"}).status_code == 422
    ran = client.post(url, headers=admin, json={"decision": "run"})
    assert ran.status_code == 200 and ran.json()["result"] == "ok" and len(SEEN) == 1
    assert client.post(url, headers=admin, json={"decision": "run"}).status_code == 409
    conn = app.state.settings.open_db()
    repo.set_skipped(conn, tid, "2026-01-02T00:00:00+00:00")
    conn.close()
    waited = client.post(url, headers=admin, json={"decision": "wait"})
    assert waited.json() == {"result": "waiting"} and len(SEEN) == 1


def test_a_rule_without_a_next_date_pauses_its_task_instead_of_stalling_the_others(
    box, monkeypatch
):
    client, admin, node, app = box
    tid = make(client, admin, node).json()["id"]
    conn = app.state.settings.open_db()
    try:
        task = repo.get(conn, tid)

        def never(spec, after):
            raise ValueError("no run found")

        monkeypatch.setattr(timing, "next_run", never)
        assert repo.advance(conn, task) is True
    finally:
        conn.close()
    after = row(app, tid)
    assert after["enabled"] == 0 and after["last_result"] == "schedule.no_next_run"


def net(name, ident, **labels):
    return {"Name": name, "Id": ident * 12, "Labels": labels}


def test_unused_networks_are_removed_but_never_those_a_stopped_project_will_need(daemon):
    NETWORKS.extend(
        [
            net("bridge", "a"),
            net("host", "b"),
            net("proxy_net", "c"),  # the network of a proxy that is stopped
            net("app_default", "d"),  # used by a running container
            net("kept", "e", **{"armadok.protect": "true"}),
            net("old_junk", "f"),
        ]
    )
    CONTAINERS.extend(
        [
            {"NetworkSettings": {"Networks": {"proxy_net": {"NetworkID": "c" * 12}}}},
            {"NetworkSettings": {"Networks": {"app_default": {"NetworkID": "d" * 12}}}},
            {"NetworkSettings": None},
            "not a container",
        ]
    )
    assert docker_control.prune({"kind": "networks"}) == {"removed": 1, "space": 0}
    assert DELETED == ["f" * 12]
    # Only listings and one delete: the daemon's own "prune everything unused" is never called.
    assert all("prune" not in path for _, path in SEEN)


def test_a_daemon_that_answers_nonsense_removes_nothing(daemon):
    NETWORKS.append({"Name": "x", "Id": "not-hex!", "Labels": None})
    NETWORKS.append("junk")
    assert docker_control.prune({"kind": "networks"}) == {"removed": 0, "space": 0}
    assert DELETED == []
