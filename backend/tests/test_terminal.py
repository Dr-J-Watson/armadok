"""Terminals: the worker side with a stand-in for Docker, and the hub's WebSocket relay."""

import base64
import json
import logging
import os
import re
import stat
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.worker import terminal_ops as ops
from armadok.worker.operation import OperationError
from tests.test_members_admin import PASSWORD, login, owner_with_node, person


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A `docker` that records its arguments and runs a plain shell for `run`."""
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1" = run ] && exec /bin/sh\nexit 0\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    stacks = tmp_path / "stacks"
    (stacks / "blog").mkdir(parents=True)
    if os.getuid() == 0:
        os.chown(stacks / "blog", 1000, 1000)  # a root-owned folder is treated apart (below)
    monkeypatch.setenv("ARMADOK_DOCKER_BIN", str(script))
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(stacks))
    monkeypatch.setenv("ARMADOK_PROJECTS_HOST_DIR", str(stacks))
    yield log, stacks
    for sid in list(ops._sessions):
        ops._end(sid)


def b64(text):
    return base64.b64encode(text.encode()).decode()


def read_until(sid, needle, seconds=5):
    seen = ""
    end = time.monotonic() + seconds
    while time.monotonic() < end and needle not in seen:
        got = ops.terminal_read({"session": sid, "wait": 0.5})
        seen += base64.b64decode(got["data"]).decode(errors="replace")
        if got["closed"]:
            break
    return seen


def runs_of(log, count=1):
    for _ in range(60):
        runs = [a for a in args_of(log) if a[0] == "run"]
        if len(runs) >= count:
            return runs
        time.sleep(0.05)
    return runs


def args_of(log):
    for _ in range(50):  # the stand-in writes its line a moment after it starts
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.05)
    return [line.split() for line in log.read_text().splitlines()]


def test_a_terminal_runs_a_shell_and_is_confined_by_the_flags_it_is_started_with(fake):
    log, stacks = fake
    done = ops.terminal_open(
        {"project": "blog", "image": "alpine:3.20", "writable": False, "network": False}
    )
    sid = done["session"]
    ops.terminal_write({"session": sid, "data": b64("echo hello-$((40+2))\n")})
    assert "hello-42" in read_until(sid, "hello-42")
    started = " ".join(runs_of(log)[0])
    for wanted in (
        "--rm", "-it", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only",
        "--network none", "--pids-limit 128", "--memory 256m", ",readonly", "-w /work",
        "--memory-swap 256m", "--entrypoint sh -- alpine:3.20",
    ):  # fmt: skip
        assert wanted in started
    assert f"--user {os.stat(stacks / 'blog').st_uid}:" in started
    ops.terminal_close({"session": sid})
    assert any(a[:2] == ["rm", "-f"] for a in args_of(log))
    with pytest.raises(OperationError):
        ops.terminal_read({"session": sid, "wait": 0})


def test_writing_and_the_network_are_only_given_when_asked_and_root_folders_stay_read_only(
    fake, monkeypatch
):
    log, stacks = fake
    done = ops.terminal_open(
        {"project": "blog", "image": "alpine", "writable": True, "network": True}
    )
    started = " ".join(runs_of(log)[0])
    assert ",readonly" not in started and "--network bridge" in started and done["writable"]
    ops.terminal_close({"session": done["session"]})
    real = os.lstat

    def as_root(path, *a, **k):
        info = real(path, *a, **k)
        return os.stat_result(
            (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, 0, 0, *info[6:])
        )

    monkeypatch.setattr(ops.os, "lstat", as_root)
    root_owned = ops.terminal_open(
        {"project": "blog", "image": "alpine", "writable": True, "network": False}
    )
    assert root_owned["writable"] is False
    for _ in range(50):
        runs = [a for a in args_of(log) if a[0] == "run"]
        if len(runs) >= 2:
            break
        time.sleep(0.05)
    assert "--user 65534:65534" in " ".join(runs[1])


def test_the_terminal_stays_off_without_the_hosts_path_and_uses_it_in_the_mount(fake, monkeypatch):
    log, stacks = fake
    base = {"project": "blog", "image": "alpine", "writable": False, "network": False}
    monkeypatch.delenv("ARMADOK_PROJECTS_HOST_DIR")
    with pytest.raises(OperationError) as caught:
        ops.terminal_open(base)
    assert caught.value.code == "worker.terminal_not_configured"
    for odd in ("relative/dir", "/", "/a,b", "/a:b", "/a/../b"):
        monkeypatch.setenv("ARMADOK_PROJECTS_HOST_DIR", odd)
        with pytest.raises(OperationError):
            ops.terminal_open(base)
    assert not log.exists()
    monkeypatch.setenv("ARMADOK_PROJECTS_HOST_DIR", "/srv/real/stacks/")
    sid = ops.terminal_open(base)["session"]
    assert "source=/srv/real/stacks/blog," in " ".join(runs_of(log)[0])
    ops.terminal_close({"session": sid})


def test_leftover_containers_of_this_worker_are_removed_once_at_first_use(fake, monkeypatch):
    log, _ = fake
    script = log.parent / "docker"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        '[ "$1" = ps ] && echo abcdef123456\n[ "$1" = run ] && exec /bin/sh\nexit 0\n'
    )
    monkeypatch.setattr(ops, "_swept", False)
    base = {"project": "blog", "image": "alpine", "writable": False, "network": False}
    first = ops.terminal_open(base)["session"]
    second = ops.terminal_open(base)["session"]
    lines = [" ".join(a) for a in args_of(log)]
    assert any(line.startswith("rm -f abcdef123456") for line in lines)
    assert sum(1 for line in lines if line.startswith("ps -aq")) == 1
    for sid in (first, second):
        ops.terminal_close({"session": sid})


@pytest.mark.parametrize(
    "bad",
    [
        {"project": "../x"}, {"project": "nope"}, {"project": "blog", "image": "-v /:/h"},
        {"project": "blog", "image": "a b"}, {"project": "blog", "image": "evil.example.net/x/y"},
        {"project": "blog", "image": "localhost/x"}, {"project": "blog", "image": "reg.io:5000/x"},
        {"project": "blog", "image": "alpine@sha256:x"},
        {"project": "blog", "writable": "yes"}, {"project": "blog", "cols": 5},
        {"project": "blog", "rows": 10000}, {"project": "blog", "network": 1},
    ],
)  # fmt: skip
def test_odd_requests_never_reach_docker(fake, bad):
    log, _ = fake
    base = {"project": "blog", "image": "alpine", "writable": False, "network": False}
    with pytest.raises(OperationError):
        ops.terminal_open({**base, **bad})
    assert not log.exists()


def test_a_link_or_a_file_is_not_a_project_folder_and_limits_hold(fake):
    log, stacks = fake
    (stacks / "evil").symlink_to("/etc")
    (stacks / "afile").write_text("x")
    base = {"image": "alpine", "writable": False, "network": False}
    for name in ("evil", "afile"):
        with pytest.raises(OperationError) as caught:
            ops.terminal_open({**base, "project": name})
        assert caught.value.code == "worker.not_found"
    first = ops.terminal_open({**base, "project": "blog"})
    ops.terminal_open({**base, "project": "blog"})
    with pytest.raises(OperationError) as caught:
        ops.terminal_open({**base, "project": "blog"})
    assert caught.value.code == "worker.busy"  # two per project
    ops.terminal_close({"session": first["session"]})


def test_input_is_bounded_and_idle_terminals_are_ended(fake):
    log, _ = fake
    sid = ops.terminal_open(
        {"project": "blog", "image": "alpine", "writable": False, "network": False}
    )["session"]
    for data in ("", b64("x" * (ops.MAX_INPUT + 1)), "!!!"):
        with pytest.raises(OperationError):
            ops.terminal_write({"session": sid, "data": data})
    for wait in (-1, 99, "x"):
        with pytest.raises(OperationError):
            ops.terminal_read({"session": sid, "wait": wait})
    ops._sessions[sid].touched = time.monotonic() - ops.IDLE_SECONDS - 1
    ops.end_stale()
    assert sid not in ops._sessions


# --- through the hub ------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path, caplog, monkeypatch, fake):
    log, stacks = fake
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
        # The folders of the fake projects directory are imported, as an owner would have done.
        from armadok.modules.projects import states

        conn = app.state.settings.open_db()
        for node in client.get("/api/v1/nodes", headers=admin).json():
            states.set_state(conn, node["id"], ["blog"], "managed")
        conn.close()
        yield client, admin, app, log


def local_node(client, headers):
    nodes = client.get("/api/v1/nodes", headers=headers).json()
    return next(n["id"] for n in nodes if n.get("local"))


def token_of(headers):
    return headers["Authorization"].split()[1]


def test_the_terminal_is_off_until_enabled_and_then_relays_input_and_output(box):
    client, admin, app, log = box
    node = local_node(client, admin)
    base = f"/api/v1/nodes/{node}/projects/blog/terminal"
    assert client.get(base, headers=admin).json()["enabled"] is False
    assert client.post(base, headers=admin, json={}).json()["error"]["code"] == "terminal.disabled"
    settings = {"enabled": True, "writable": False, "network": False, "image": "alpine:3.20"}
    assert client.put(base, headers=admin, json=settings).status_code == 204
    opened = client.post(base, headers=admin, json={"cols": 100, "rows": 30})
    assert opened.status_code == 201
    ticket = opened.json()["ticket"]
    with client.websocket_connect(f"/api/v1/terminal/{ticket}") as ws:
        ws.send_text(json.dumps({"token": token_of(admin)}))
        ws.send_text(json.dumps({"in": b64("echo relay-$((6*7))\n")}))
        ws.send_text(json.dumps({"resize": [120, 40]}))
        seen = ""
        for _ in range(40):
            message = json.loads(ws.receive_text())
            seen += base64.b64decode(message.get("out", "")).decode(errors="replace")
            if "relay-42" in seen:
                break
        assert "relay-42" in seen
        ws.send_text(json.dumps({"bye": True}))  # anything unknown ends the terminal
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    # The ticket works once.
    with pytest.raises(WebSocketDisconnect):  # closed with 4404 before accepting
        with client.websocket_connect(f"/api/v1/terminal/{ticket}"):
            pass
    for _ in range(60):  # the server finishes closing a moment after the screen goes
        audit = client.get("/api/v1/audit", headers=admin).text
        if "terminal.close" in audit:
            break
        time.sleep(0.1)
    assert "terminal.open" in audit and "terminal.close" in audit and "relay-42" not in audit


def test_only_the_opener_with_a_valid_token_may_use_a_ticket(box):
    client, admin, app, log = box
    node = local_node(client, admin)
    base = f"/api/v1/nodes/{node}/projects/blog/terminal"
    client.put(base, headers=admin, json={"enabled": True})
    ticket = client.post(base, headers=admin, json={}).json()["ticket"]
    _, pat = person(client, admin, "pat")
    with client.websocket_connect(f"/api/v1/terminal/{ticket}") as ws:
        ws.send_text(json.dumps({"token": token_of(pat)}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    # Somebody else knocking does not use the ticket up: the person it was made for still can.
    with client.websocket_connect(f"/api/v1/terminal/{ticket}") as ws:
        ws.send_text(json.dumps({"token": token_of(admin)}))
        ws.send_text(json.dumps({"in": b64("echo ok-$((1+1))\n")}))
        seen = ""
        for _ in range(40):
            seen += base64.b64decode(json.loads(ws.receive_text()).get("out", "")).decode(
                errors="replace"
            )
            if "ok-2" in seen:
                break
        assert "ok-2" in seen
        ws.send_text(json.dumps({"bye": True}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    # Nothing is left running when the screen goes.
    for _ in range(60):
        if any(a[:2] == ["rm", "-f"] for a in args_of(log)):
            break
        time.sleep(0.1)
    assert any(a[:2] == ["rm", "-f"] for a in args_of(log))


def test_a_ticket_nobody_used_expires_and_a_late_one_is_refused(box, monkeypatch):
    from armadok.modules.terminal import routes

    client, admin, app, log = box
    node = local_node(client, admin)
    base = f"/api/v1/nodes/{node}/projects/blog/terminal"
    client.put(base, headers=admin, json={"enabled": True})
    ticket = client.post(base, headers=admin, json={}).json()["ticket"]
    monkeypatch.setattr(routes, "TICKET_SECONDS", -1)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/terminal/{ticket}"):
            pass


def test_rights_keys_and_second_factor(box):
    client, admin, app, log = box
    _, olga, node = owner_with_node(client, admin)
    from armadok.modules.projects import states

    conn = app.state.settings.open_db()
    states.set_state(conn, node, ["blog"], "managed")
    conn.close()
    base = f"/api/v1/nodes/{node}/projects/blog/terminal"
    pat_id, pat = person(client, admin, "pat")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    )
    assert client.get(base, headers=pat).status_code == 200
    assert client.put(base, headers=pat, json={"enabled": True}).status_code == 403
    assert client.post(base, headers=pat, json={}).status_code == 403
    made = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["projects.terminal"]}
    )
    assert made.status_code >= 400  # a sensitive right cannot be put on a key
    bad = client.put(
        f"/api/v1/nodes/{node}/projects/../terminal", headers=olga, json={"enabled": True}
    )
    assert bad.status_code in (404, 422)
    assert (
        client.put(base, headers=olga, json={"enabled": True, "image": "-v /:/h"}).status_code
        == 422
    )
