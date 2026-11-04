import asyncio
import logging
import re
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.uptime import repository as repo
from armadok.modules.uptime import routes as uptime_routes
from armadok.modules.uptime import service, watch
from armadok.worker import uptime_ops
from armadok.worker.operation import OperationError

PASSWORD = "correct horse battery"


class Target(BaseHTTPRequestHandler):
    """A tiny site: `/ok` answers 200, `/down` 503, `/moved` redirects."""

    def do_GET(self):
        code = {"/ok": 200, "/down": 503, "/moved": 302}.get(self.path.split("?")[0], 404)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "http://192.0.2.1/")
        self.end_headers()
        self.wfile.write(b"secret body")

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = HTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(uptime_routes, "MIN_CHECK_GAP_SECONDS", 0)
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
    for right, scope in grants:
        client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    return login(client, name)


def make(client, admin, node, **extra):
    body = {"name": "site", "kind": "http", "url": "http://127.0.0.1:1/ok", **extra}
    return client.post(f"/api/v1/nodes/{node}/uptime", headers=admin, json=body)


# --- the worker side -------------------------------------------------------------------------


def test_a_tcp_probe_tells_open_from_closed(site):
    assert uptime_ops.probe_tcp({"host": "127.0.0.1", "port": site})["status"] is None
    with pytest.raises(OperationError) as error:
        uptime_ops.probe_tcp({"host": "127.0.0.1", "port": closed_port()})
    assert error.value.code == "worker.probe_refused"


def test_an_http_probe_returns_the_status_and_nothing_else(site):
    answer = uptime_ops.probe_http(
        {"host": "127.0.0.1", "port": site, "scheme": "http", "path": "/down"}
    )
    assert answer["status"] == 503 and set(answer) == {"status", "latency_ms"}


def test_redirects_are_not_followed(site):
    answer = uptime_ops.probe_http(
        {"host": "127.0.0.1", "port": site, "scheme": "http", "path": "/moved"}
    )
    assert answer["status"] == 302


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "fe80::1",
        "0.0.0.0",
        "224.0.0.1",
        "::ffff:a9fe:a9fe",
        "fd00:ec2::254",
        "100.100.100.200",
        "2002:a9fe:a9fe::1",
        "64:ff9b::a9fe:a9fe",
    ],
)
def test_addresses_that_must_never_be_probed_are_refused(host):
    with pytest.raises(OperationError) as error:
        uptime_ops.probe_tcp({"host": host, "port": 80})
    assert error.value.code == "worker.probe_address_not_allowed"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.0.0.5", "192.168.1.1", "fd12::1"])
def test_loopback_and_private_addresses_are_what_gets_monitored(host):
    assert uptime_ops.refused(host) is False


def drip_server(delay: float, port_holder: list):
    """A server that sends the answer one byte at a time, never finishing the status line."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port_holder.append(srv.getsockname()[1])

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=feed, args=(conn,), daemon=True).start()

    def feed(conn):
        try:
            conn.recv(1024)
            for _ in range(200):
                conn.sendall(b"X")
                time.sleep(delay)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_a_target_that_drips_bytes_cannot_hold_a_probe_past_its_deadline():
    port = []
    srv = drip_server(0.3, port)
    started = time.monotonic()
    with pytest.raises(OperationError):
        uptime_ops.probe_http(
            {"host": "127.0.0.1", "port": port[0], "scheme": "http", "path": "/", "timeout": 1}
        )
    assert time.monotonic() - started < 3
    srv.close()


def test_a_name_that_never_resolves_is_cut_off_at_the_deadline(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: time.sleep(5))
    started = time.monotonic()
    with pytest.raises(OperationError) as error:
        uptime_ops.probe_tcp({"host": "slow.example.com", "port": 80, "timeout": 1})
    assert error.value.code == "worker.probe_timeout" and time.monotonic() - started < 3


def test_only_a_few_probes_run_at_once_on_a_worker(monkeypatch):
    held = []
    for _ in range(uptime_ops.MAX_AT_ONCE):
        assert uptime_ops._slots.acquire(blocking=False)
        held.append(1)
    try:
        with pytest.raises(OperationError) as error:
            uptime_ops.probe_tcp({"host": "127.0.0.1", "port": 80})
        assert error.value.code == "worker.busy"
    finally:
        for _ in held:
            uptime_ops._slots.release()


def test_a_garbled_status_line_is_a_failed_probe():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.recv(1024)
        conn.sendall(b"HTTP/1.1 99999 nope\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(OperationError) as error:
        uptime_ops.probe_http(
            {"host": "127.0.0.1", "port": srv.getsockname()[1], "scheme": "http", "path": "/"}
        )
    assert error.value.code == "worker.probe_failed"
    srv.close()


@pytest.mark.parametrize(
    "args",
    [
        {"host": "127.0.0.1", "port": 0, "scheme": "http"},
        {"host": "127.0.0.1", "port": 80, "scheme": "ftp"},
        {"host": "user@127.0.0.1", "port": 80, "scheme": "http"},
        {"host": "127.0.0.1", "port": 80, "scheme": "http", "path": "/a b"},
        {"host": "127.0.0.1", "port": 80, "scheme": "http", "path": "x"},
        {"host": "127.0.0.1", "port": 80, "scheme": "http", "timeout": 600},
        {"host": "127.0.0.1", "port": True, "scheme": "http"},
    ],
)
def test_malformed_probe_arguments_are_refused(args):
    with pytest.raises(OperationError) as error:
        uptime_ops.probe_http(args)
    assert error.value.code == "worker.invalid_path"


# --- the API ----------------------------------------------------------------------------------


def test_rights_and_visibility(box):
    client, admin, node, _ = box
    scope = f"node:{node}"
    reader = person(client, admin, "reader", [("nodes.read", scope), ("uptime.read", scope)])
    stranger = person(client, admin, "stranger")
    assert make(client, admin, node).status_code == 201
    assert client.get(f"/api/v1/nodes/{node}/uptime", headers=reader).status_code == 200
    assert make(client, reader, node).status_code == 403
    # A machine the person cannot see does not exist for them.
    assert client.get(f"/api/v1/nodes/{node}/uptime", headers=stranger).status_code == 404
    assert make(client, stranger, node).status_code == 404


def test_a_key_cannot_manage_probes(box):
    client, admin, node, _ = box
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["uptime.manage"]}
    )
    assert key.status_code == 422 and key.json()["error"]["code"] == "apikeys.invalid_right"


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "ftp://example.com/"},
        {"url": "http://user:pw@example.com/"},
        {"url": "http://example.com/#frag"},
        {"url": "http://example.com/health?token=abc"},
        {"name": "   "},
        {"url": "http://example.com:99999/"},
        {"url": "http://example.com/a b"},
        {"url": None},
        {"interval_seconds": 5},
        {"kind": "tcp", "url": None},
        {"unknown": 1},
    ],
)
def test_invalid_targets_are_refused(box, extra):
    client, admin, node, _ = box
    assert make(client, admin, node, **extra).status_code == 422


def test_link_local_addresses_are_refused_at_creation(box):
    client, admin, node, _ = box
    got = make(client, admin, node, url="http://169.254.169.254/latest")
    assert got.status_code == 422 and got.json()["error"]["code"] == "uptime.address_not_allowed"
    tcp = make(client, admin, node, kind="tcp", url=None, host="169.254.169.254", port=80)
    assert tcp.status_code == 422


def test_the_number_of_probes_is_capped(box, monkeypatch):
    client, admin, node, _ = box
    monkeypatch.setattr(uptime_routes, "MAX_PROBES_PER_NODE", 2)
    assert make(client, admin, node).status_code == 201
    assert make(client, admin, node).status_code == 201
    assert make(client, admin, node).json()["error"]["code"] == "uptime.too_many"


def test_change_pause_and_delete(box):
    client, admin, node, _ = box
    pid = make(client, admin, node).json()["id"]
    url = f"/api/v1/nodes/{node}/uptime/{pid}"
    changed = client.patch(url, headers=admin, json={"enabled": False, "name": "renamed"})
    assert changed.json()["enabled"] is False and changed.json()["name"] == "renamed"
    assert client.patch(url, headers=admin, json={"host": "x"}).status_code == 422
    assert client.delete(url, headers=admin).status_code == 204
    assert client.delete(url, headers=admin).status_code == 404


def test_a_probe_of_another_machine_is_not_found(box):
    client, admin, node, app = box
    pid = make(client, admin, node).json()["id"]
    assert client.delete(f"/api/v1/nodes/other/uptime/{pid}", headers=admin).status_code == 404


def test_checking_now_records_the_result_and_the_history(box, site):
    client, admin, node, _ = box
    pid = make(client, admin, node, url=f"http://127.0.0.1:{site}/ok").json()["id"]
    got = client.post(f"/api/v1/nodes/{node}/uptime/{pid}/check", headers=admin)
    assert got.status_code == 200 and got.json()["state"] == "up"
    listed = client.get(f"/api/v1/nodes/{node}/uptime", headers=admin).json()["probes"][0]
    assert listed["availability"] == 100 and listed["recent"][0]["ok"] is True
    assert "secret body" not in str(listed)


def test_audit_records_probe_changes_without_the_address(box):
    client, admin, node, _ = box
    pid = make(client, admin, node, url="http://intranet.example.com/").json()["id"]
    client.delete(f"/api/v1/nodes/{node}/uptime/{pid}", headers=admin)
    log = client.get("/api/v1/audit", headers=admin).text
    assert "uptime.create" in log and "uptime.delete" in log
    assert "intranet.example.com" not in log


# --- the checker ------------------------------------------------------------------------------


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def probe_row(app, pid):
    conn = app.state.settings.open_db()
    try:
        return repo.get(conn, pid)
    finally:
        conn.close()


def test_a_site_is_down_after_two_failures_and_back_after_one_success(box, site, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload["event"])

    monkeypatch.setattr(service, "emit", fake_emit)
    pid = make(client, admin, node, url=f"http://127.0.0.1:{site}/down").json()["id"]

    async def go(times):
        for _ in range(times):
            await service.check(app, probe_row(app, pid))

    # The test client runs the app on its own loop; the checks use the same worker call path.
    client.portal.call(go, 1)
    assert probe_row(app, pid)["state"] == "unknown" and sent == []
    client.portal.call(go, 1)
    assert probe_row(app, pid)["state"] == "down" and sent == ["uptime.down"]
    client.portal.call(go, 3)
    assert sent == ["uptime.down"]  # said once, not at each failure
    # The site comes back: same probe, an address that answers.
    conn = app.state.settings.open_db()
    conn.execute("UPDATE probes SET path = '/ok' WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    client.portal.call(go, 1)
    assert probe_row(app, pid)["state"] == "down"  # one good check is not yet a recovery
    client.portal.call(go, 1)
    assert probe_row(app, pid)["state"] == "up" and sent == ["uptime.down", "uptime.up"]


def test_an_unreachable_target_counts_but_an_unreachable_worker_does_not(box, monkeypatch):
    client, admin, node, app = box
    pid = make(
        client, admin, node, kind="tcp", url=None, host="127.0.0.1", port=closed_port()
    ).json()["id"]
    assert client.post(f"/api/v1/nodes/{node}/uptime/{pid}/check", headers=admin).status_code == 200
    assert probe_row(app, pid)["failures"] == 1
    assert probe_row(app, pid)["last_reason"] == "refused"
    # The node cannot be asked at all: nothing is recorded, the state is untouched.
    conn = app.state.settings.open_db()
    conn.execute(
        "UPDATE nodes SET suspended_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (node,)
    )
    conn.commit()
    conn.close()
    assert client.portal.call(service.check, app, probe_row(app, pid)) is None
    assert probe_row(app, pid)["failures"] == 1


def test_an_unexpected_status_can_be_the_expected_one(box, site):
    client, admin, node, app = box
    pid = make(client, admin, node, url=f"http://127.0.0.1:{site}/down", expect_status=503).json()[
        "id"
    ]
    got = client.post(f"/api/v1/nodes/{node}/uptime/{pid}/check", headers=admin)
    assert got.json()["state"] == "up"


def test_only_due_probes_run_and_a_round_is_bounded(box, site):
    client, admin, node, app = box
    for _ in range(3):
        make(client, admin, node, url=f"http://127.0.0.1:{site}/ok")
    conn = app.state.settings.open_db()
    try:
        assert len(repo.due(conn, datetime.now(UTC), 10)) == 3
        assert len(repo.due(conn, datetime.now(UTC), 2)) == 2
        for row in repo.list_for_node(conn, node):
            repo.mark_checked(conn, row["id"])
        assert repo.due(conn, datetime.now(UTC), 10) == []
        later = datetime.now(UTC) + timedelta(seconds=61)
        assert len(repo.due(conn, later, 10)) == 3
    finally:
        conn.close()
    assert client.portal.call(watch.run_due, app) == 0


def test_the_history_is_trimmed(box, monkeypatch):
    client, admin, node, app = box
    pid = make(client, admin, node).json()["id"]
    monkeypatch.setattr(repo, "MAX_RESULTS", 5)
    conn = app.state.settings.open_db()
    try:
        for i in range(9):
            repo.record_result(conn, pid, True, i, None)
        assert len(repo.history(conn, pid, 100)) == 5
        old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        conn.execute("UPDATE probe_results SET at = ?", (old,))
        repo.record_result(conn, pid, True, 1, None)
        assert len(repo.history(conn, pid, 100)) == 1
    finally:
        conn.close()


def test_the_list_says_whether_the_caller_can_manage(box):
    client, admin, node, _ = box
    scope = f"node:{node}"
    reader = person(client, admin, "reader", [("nodes.read", scope), ("uptime.read", scope)])
    url = f"/api/v1/nodes/{node}/uptime"
    assert client.get(url, headers=admin).json()["can_manage"] is True
    assert client.get(url, headers=reader).json()["can_manage"] is False


def test_a_lying_worker_cannot_make_a_probe_succeed_or_break_the_checker(box, monkeypatch):
    client, admin, node, app = box
    pid = make(client, admin, node).json()["id"]

    async def liar(*_a, **_k):
        return {"status": -1, "latency_ms": 2**70}

    monkeypatch.setattr(service, "call_operation", liar)
    for _ in range(2):
        client.portal.call(service.check, app, probe_row(app, pid))
    row = probe_row(app, pid)
    assert row["state"] == "down" and row["last_reason"] == "failed"

    async def huge(*_a, **_k):
        return {"status": 99999}

    monkeypatch.setattr(service, "call_operation", huge)
    client.portal.call(service.check, app, probe_row(app, pid))
    assert probe_row(app, pid)["last_reason"] == "failed"


def test_one_good_check_between_failures_does_not_flap_the_alerts(box, site, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload["event"])

    monkeypatch.setattr(service, "emit", fake_emit)
    pid = make(client, admin, node, url=f"http://127.0.0.1:{site}/down").json()["id"]

    def set_path(path):
        conn = app.state.settings.open_db()
        conn.execute("UPDATE probes SET path = ? WHERE id = ?", (path, pid))
        conn.commit()
        conn.close()

    for path in ("/down", "/down", "/ok", "/down", "/ok", "/down"):
        set_path(path)
        client.portal.call(service.check, app, probe_row(app, pid))
    assert sent == ["uptime.down"]


def test_pausing_forgets_the_state_and_a_check_in_progress_is_not_doubled(box, site):
    client, admin, node, app = box
    pid = make(client, admin, node, url=f"http://127.0.0.1:{site}/down").json()["id"]
    url = f"/api/v1/nodes/{node}/uptime/{pid}"
    for _ in range(2):
        client.post(url + "/check", headers=admin)
    assert probe_row(app, pid)["state"] == "down"
    client.patch(url, headers=admin, json={"enabled": False})
    assert probe_row(app, pid)["state"] == "unknown"
    client.patch(url, headers=admin, json={"enabled": True})
    service._in_flight.add(pid)
    try:
        assert client.post(url + "/check", headers=admin).status_code == 409
        assert client.portal.call(service.check, app, probe_row(app, pid)) is None
    finally:
        service._in_flight.discard(pid)


def test_a_manual_check_is_audited_without_the_address(box, site):
    client, admin, node, _ = box
    pid = make(client, admin, node, url=f"http://127.0.0.1:{site}/ok").json()["id"]
    client.post(f"/api/v1/nodes/{node}/uptime/{pid}/check", headers=admin)
    log = client.get("/api/v1/audit", headers=admin).text
    assert "uptime.check" in log and str(site) not in log


def test_the_platform_wide_cap_and_non_blocking_rounds(box, site, monkeypatch):
    client, admin, node, app = box
    monkeypatch.setattr(uptime_routes, "MAX_PROBES", 1)
    assert make(client, admin, node).status_code == 201
    assert make(client, admin, node).json()["error"]["code"] == "uptime.too_many"

    async def slow(_app, probe):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(service, "check", slow)
    started = time.monotonic()
    assert client.portal.call(watch.run_due, app) == 1
    assert time.monotonic() - started < 0.4  # the round did not wait for the check


def test_the_dotted_mapped_form_is_not_a_valid_host_and_is_refused_all_the_same():
    assert uptime_ops.refused("::ffff:169.254.169.254") is True
    with pytest.raises(OperationError):
        uptime_ops.probe_tcp({"host": "::ffff:169.254.169.254", "port": 80})


def test_a_probe_is_linked_to_the_domain_it_watches(box):
    from armadok.core.module import RelationData
    from armadok.modules.uptime import relations

    client, admin, node, app = box
    made = make(client, admin, node, name="Blog", url="http://blog.example.com/ok")
    assert made.status_code == 201, made.text
    tcp = make(client, admin, node, name="Port", kind="tcp", url=None, host="127.0.0.1", port=80)
    data = asyncio.run(relations.provide(app, node, None))
    assert isinstance(data, RelationData)
    labels = {o.label for o in data.objects}
    assert any(label.startswith("Blog (") for label in labels)
    # Only the web probe points at a domain; a port probe has nothing to link to.
    assert [(link.target, link.kind) for link in data.links] == [
        (("domain", "blog.example.com"), "watches")
    ]
    assert tcp.status_code in (201, 422)
