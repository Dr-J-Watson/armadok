import asyncio
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from armadok.core import outbound
from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.notes.reminders import send_due_reminders
from armadok.modules.notifications import delivery

PASSWORD = "correct horse battery"
MODULES = ("users", "nodes", "workers", "audit", "notes", "notifications")


class Sink(BaseHTTPRequestHandler):
    """A local receiver standing in for a webhook / ntfy server."""

    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        Sink.received.append(
            {"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)}
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def sink():
    Sink.received = []
    server = HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def build(tmp_path, caplog, allow_private=False):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                encryption_key=generate_key(),
                allow_private_outbound=allow_private,
            ),
            load_registry(MODULES),
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    client = TestClient(app)
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    return client, app


def login(client, user="admin"):
    token = client.post("/api/v1/auth/login", json={"username": user, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def raise_event(app, event, **kwargs):
    from armadok.core.events import emit
    from armadok.modules.notifications.service import wait_for_deliveries

    async def go():
        await emit(app, "notify", event=event, **kwargs)
        await wait_for_deliveries(app)  # deliveries run in the background: wait for them here

    asyncio.run(go())


def test_inbox_in_the_account_language_and_read_state(tmp_path, caplog):
    client, app = build(tmp_path, caplog)
    admin = login(client)
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    raise_event(app, "worker.disconnected", node_id=node, params={"node": "home"})
    entry = client.get("/api/v1/notifications", headers=admin).json()
    assert entry["unread"] == 1 and entry["entries"][0]["title"] == "Worker disconnected"
    assert "home" in entry["entries"][0]["body"]
    assert (
        client.patch("/api/v1/auth/language", headers=admin, json={"language": "fr"}).status_code
        == 204
    )
    fr = client.get("/api/v1/notifications", headers=admin).json()["entries"][0]
    assert fr["title"] == "Worker déconnecté"
    assert client.post("/api/v1/notifications/read", headers=admin, json={}).status_code == 204
    assert client.get("/api/v1/notifications/unread-count", headers=admin).json() == {"unread": 0}
    assert (
        client.patch("/api/v1/auth/language", headers=admin, json={"language": "xx"}).status_code
        == 422
    )


def test_nobody_is_told_about_a_node_they_cannot_see(tmp_path, caplog):
    client, app = build(tmp_path, caplog)
    admin = login(client)
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "secret-home", "type": "docker"}
    ).json()["id"]
    client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    bob = login(client, "bob")
    raise_event(app, "worker.disconnected", node_id=node, params={"node": "secret-home"})
    assert client.get("/api/v1/notifications", headers=bob).json()["entries"] == []
    assert len(client.get("/api/v1/notifications", headers=admin).json()["entries"]) == 1


def test_rules_decide_where_and_from_which_severity(tmp_path, caplog, sink):
    client, app = build(tmp_path, caplog, allow_private=True)
    admin = login(client)
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    channel = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "ntfy",
            "name": "phone",
            "settings": {"server": f"http://127.0.0.1:{sink}", "topic": "alerts"},
        },
    ).json()["id"]
    rules = [
        {
            "event": "worker.disconnected",
            "target": channel,
            "min_severity": "warning",
            "node_id": node,
            "enabled": True,
        },
        {
            "event": "worker.connected",
            "target": channel,
            "min_severity": "critical",
            "node_id": None,
            "enabled": True,
        },
    ]
    assert (
        client.put("/api/v1/notifications/rules", headers=admin, json={"rules": rules}).status_code
        == 200
    )
    raise_event(app, "worker.disconnected", node_id=node, params={"node": "home"})
    raise_event(app, "worker.connected", node_id=node, params={"node": "home"})  # info < critical
    assert len(Sink.received) == 1 and Sink.received[0]["path"] == "/alerts"
    assert Sink.received[0]["headers"]["Priority"] == "4"
    # With a rule that only names the phone, the inbox is no longer used for that event.
    assert client.get("/api/v1/notifications", headers=admin).json()["entries"] == []
    bad = [
        {
            "event": "nope",
            "target": "in_app",
            "min_severity": "info",
            "node_id": None,
            "enabled": True,
        }
    ]
    assert (
        client.put("/api/v1/notifications/rules", headers=admin, json={"rules": bad}).status_code
        == 422
    )
    foreign = [
        {
            "event": "worker.connected",
            "target": "someone-elses",
            "min_severity": "info",
            "node_id": None,
            "enabled": True,
        }
    ]
    assert (
        client.put(
            "/api/v1/notifications/rules", headers=admin, json={"rules": foreign}
        ).status_code
        == 422
    )


def test_webhook_is_signed_and_the_secret_is_shown_once_and_stored_encrypted(
    tmp_path, caplog, sink
):
    import sqlite3

    client, app = build(tmp_path, caplog, allow_private=True)
    admin = login(client)
    created = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "webhook",
            "name": "ci",
            "settings": {"url": f"http://127.0.0.1:{sink}/hook"},
        },
    ).json()
    secret = created["secret"]
    assert (
        secret
        and "secret" not in client.get("/api/v1/notifications/channels", headers=admin).json()[0]
    )
    assert "url" not in str(client.get("/api/v1/notifications/channels", headers=admin).json())
    stored = (
        sqlite3.connect(tmp_path / "armadok.db")
        .execute("SELECT config FROM notification_channels")
        .fetchone()[0]
    )
    assert secret not in stored and "127.0.0.1" not in stored
    assert client.post(
        f"/api/v1/notifications/channels/{created['id']}/test", headers=admin
    ).json() == {"status": "ok"}
    got = Sink.received[0]
    stamp = got["headers"]["X-Armadok-Timestamp"]
    assert got["headers"]["X-Armadok-Signature"] == "sha256=" + delivery.sign(
        secret, stamp, got["body"]
    )
    assert json.loads(got["body"])["event"] == "test"
    assert (
        client.get("/api/v1/notifications/channels", headers=admin).json()[0]["last_status"] == "ok"
    )


def test_private_and_odd_destinations_are_refused_by_default(tmp_path, caplog, sink):
    client, _ = build(tmp_path, caplog)  # private addresses NOT allowed
    admin = login(client)
    for bad in (
        "file:///etc/passwd",
        "http://user:pw@example.com/x",
        "ftp://example.com",
        "http://",
    ):
        r = client.post(
            "/api/v1/notifications/channels",
            headers=admin,
            json={"kind": "webhook", "name": "x", "settings": {"url": bad}},
        )
        assert r.status_code == 422, bad
    created = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "webhook",
            "name": "internal",
            "settings": {"url": f"http://127.0.0.1:{sink}/x"},
        },
    ).json()
    test = client.post(f"/api/v1/notifications/channels/{created['id']}/test", headers=admin)
    assert test.status_code == 502 and test.json()["error"]["code"] == "outbound.failed"
    # The owner of the channel can see why in its status (the policy is not a secret).
    listed = client.get("/api/v1/notifications/channels", headers=admin).json()
    assert listed[0]["last_status"] == "outbound.address_not_allowed"
    assert Sink.received == []  # nothing was sent to the loopback receiver


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fe80::1", "0.0.0.0"],
)
def test_non_public_addresses_are_not_public(address):
    assert outbound._public(address) is False
    assert outbound._public("93.184.216.34") is True


def test_channels_are_private_to_their_owner(tmp_path, caplog, sink):
    client, _ = build(tmp_path, caplog, allow_private=True)
    admin = login(client)
    client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    cid = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "discord", "name": "d", "settings": {"url": f"http://127.0.0.1:{sink}/d"}},
    ).json()["id"]
    bob = login(client, "bob")
    assert client.get("/api/v1/notifications/channels", headers=bob).json() == []
    assert client.post(f"/api/v1/notifications/channels/{cid}/test", headers=bob).status_code == 404
    assert client.delete(f"/api/v1/notifications/channels/{cid}", headers=bob).status_code == 404


def test_note_reminders_notify_the_owner_without_the_note_text(tmp_path, caplog):
    client, app = build(tmp_path, caplog)
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    note = client.post(
        "/api/v1/notes",
        headers=admin,
        json={
            "node_id": node,
            "object_type": "container",
            "object_id": "web",
            "body": "my private worry",
            "remind_at": "2020-01-01T00:00:00Z",
        },
    ).json()
    assert asyncio.run(send_due_reminders(app)) == 1
    assert asyncio.run(send_due_reminders(app)) == 0  # fires once
    entry = client.get("/api/v1/notifications", headers=admin).json()["entries"][0]
    assert entry["title"] == "Note reminder" and "container web" in entry["body"]
    assert "worry" not in json.dumps(entry)
    # Setting a new date makes it due again; a note marked done is left alone.
    client.patch(
        f"/api/v1/notes/{note['id']}", headers=admin, json={"remind_at": "2020-02-01T00:00:00Z"}
    )
    assert asyncio.run(send_due_reminders(app)) == 1
    client.patch(
        f"/api/v1/notes/{note['id']}",
        headers=admin,
        json={"remind_at": "2020-03-01T00:00:00Z", "done": True},
    )
    assert asyncio.run(send_due_reminders(app)) == 0


def test_a_flapping_worker_does_not_become_a_stream_of_notifications(tmp_path, caplog):
    client, app = build(tmp_path, caplog)
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    for _ in range(5):
        raise_event(app, "worker.disconnected", node_id=node, params={"node": "n"})
    assert (
        len(client.get("/api/v1/notifications", headers=admin).json()["entries"]) == 1
    )  # cooldown
    raise_event(
        app, "worker.connected", node_id=node, params={"node": "n"}
    )  # another event: allowed
    assert len(client.get("/api/v1/notifications", headers=admin).json()["entries"]) == 2


def test_control_characters_in_names_cannot_break_headers_or_forge_lines(tmp_path, caplog, sink):
    client, app = build(tmp_path, caplog, allow_private=True)
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    cid = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "ntfy",
            "name": "p",
            "settings": {"server": f"http://127.0.0.1:{sink}", "topic": "t"},
        },
    ).json()["id"]
    client.put(
        "/api/v1/notifications/rules",
        headers=admin,
        json={
            "rules": [
                {
                    "event": "worker.disconnected",
                    "target": cid,
                    "min_severity": "info",
                    "node_id": None,
                    "enabled": True,
                }
            ]
        },
    )
    raise_event(
        app,
        "worker.disconnected",
        node_id=node,
        params={"node": "evil\r\nX-Injected: 1\n@everyone"},
    )
    assert len(Sink.received) == 1
    got = Sink.received[0]
    assert (
        "X-Injected" not in got["headers"] and "\n" not in got["body"].decode().split("evil")[1][:3]
    )
    bad_token = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "ntfy",
            "name": "q",
            "settings": {"topic": "t", "token": "abc\r\nHost: evil"},
        },
    )
    assert bad_token.status_code == 422


def test_discord_messages_cannot_ping_everyone_and_webhooks_carry_a_delivery_id(
    tmp_path, caplog, sink
):
    client, app = build(tmp_path, caplog, allow_private=True)
    admin = login(client)
    node = client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"}).json()[
        "id"
    ]
    ids = {}
    for kind in ("discord", "webhook"):
        ids[kind] = client.post(
            "/api/v1/notifications/channels",
            headers=admin,
            json={
                "kind": kind,
                "name": kind,
                "settings": {"url": f"http://127.0.0.1:{sink}/{kind}"},
            },
        ).json()["id"]
    client.put(
        "/api/v1/notifications/rules",
        headers=admin,
        json={
            "rules": [
                {
                    "event": "worker.connected",
                    "target": t,
                    "min_severity": "info",
                    "node_id": None,
                    "enabled": True,
                }
                for t in ids.values()
            ]
        },
    )
    raise_event(app, "worker.connected", node_id=node, params={"node": "@everyone"})
    by_path = {r["path"]: r for r in Sink.received}
    assert json.loads(by_path["/discord"]["body"])["allowed_mentions"] == {"parse": []}
    assert len(by_path["/webhook"]["headers"]["X-Armadok-Delivery"]) == 32


def test_the_test_endpoint_is_rate_limited_and_does_not_reveal_the_target_answer(
    tmp_path, caplog, sink
):
    client, _ = build(tmp_path, caplog, allow_private=True)
    admin = login(client)
    cid = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "discord", "name": "d", "settings": {"url": f"http://127.0.0.1:{sink}/d"}},
    ).json()["id"]
    assert (
        client.post(f"/api/v1/notifications/channels/{cid}/test", headers=admin).status_code == 200
    )
    again = client.post(f"/api/v1/notifications/channels/{cid}/test", headers=admin)
    assert again.status_code == 429 and again.json()["error"]["code"] == "notifications.too_fast"


def test_more_addresses_that_reach_internal_hosts_are_refused():
    for address in (
        "64:ff9b::7f00:1",
        "192.0.0.9",
        "::ffff:8.8.8.8",
        "::ffff:127.0.0.1",
        "100.64.0.1",
        "fd00::1",
    ):
        assert outbound._public(address) is False, address


def test_a_slow_or_endless_receiver_is_cut_off_by_the_deadline(monkeypatch):
    async def never_answers(self, *args, **kwargs):  # stands in for a server that drips bytes
        await asyncio.sleep(60)

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda *a, **k: _Hang())
    monkeypatch.setattr(outbound, "DEADLINE_SECONDS", 0.3)

    class _Hang:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, *exc):
            return False

    async def go():
        monkeypatch.setattr(outbound, "_resolve", lambda *a: _fake_public())
        with pytest.raises(outbound.OutboundError) as caught:
            await outbound.post("http://example.com/x", b"{}", {})
        assert caught.value.code == "outbound.failed"

    async def _fake_public():
        return "93.184.216.34"

    asyncio.run(go())
