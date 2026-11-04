import asyncio
import datetime as dt
import logging
import re
import socket
import ssl
import threading
from datetime import UTC, datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from armadok.core import outbound
from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.notifications import delivery, quiet

PASSWORD = "correct horse battery"
MODULES = ("users", "nodes", "workers", "audit", "notes", "notifications", "relations", "uptime")


class SmtpServer:
    """A tiny SMTP server that records messages. `starttls=False`: it does not offer STARTTLS."""

    def __init__(self, tls_context: ssl.SSLContext | None = None) -> None:
        self.messages: list[str] = []
        self.commands: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.context = tls_context
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn: socket.socket) -> None:
        try:
            if self.context is not None:
                conn = self.context.wrap_socket(conn, server_side=True)
            reader = conn.makefile("rb")
            conn.sendall(b"220 fake ESMTP\r\n")
            data = None
            while True:
                line = reader.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip("\r\n")
                if data is not None:
                    if text == ".":
                        self.messages.append("\n".join(data))
                        data = None
                        conn.sendall(b"250 queued\r\n")
                    else:
                        data.append(text)
                    continue
                self.commands.append(text.split(" ")[0].upper())
                verb = text.upper()
                if verb.startswith("EHLO"):
                    conn.sendall(b"250-fake\r\n250 8BITMIME\r\n")
                elif verb.startswith("DATA"):
                    data = []
                    conn.sendall(b"354 go\r\n")
                elif verb.startswith("QUIT"):
                    conn.sendall(b"221 bye\r\n")
                    return
                else:
                    conn.sendall(b"250 ok\r\n")
        except (OSError, ssl.SSLError):
            return


@pytest.fixture
def smtp():
    server = SmtpServer()
    yield server
    server.close()


def mail_config(port, **extra):
    return {
        "kind": "email",
        "host": "127.0.0.1",
        "port": port,
        "security": "none",
        "sender": "armadok@example.com",
        "recipient": "me@example.com",
        **extra,
    }


def send(config, title="Title", body="Body", allow_private=True):
    return asyncio.run(
        delivery.send(
            config,
            event="e",
            severity="info",
            title=title,
            body=body,
            node_id=None,
            allow_private=allow_private,
        )
    )


def test_a_message_is_delivered_with_a_subject_and_a_body(smtp):
    send(mail_config(smtp.port), title="Site unavailable", body="On home: blog does not respond.")
    (message,) = smtp.messages
    assert "Subject: [Armadok] Site unavailable" in message
    assert "From: armadok@example.com" in message and "To: me@example.com" in message
    assert "On home: blog does not respond." in message


def test_a_line_break_cannot_add_a_header_or_a_recipient(smtp):
    for title in ("Hello\r\nBcc: victim@example.com", "Hello\nBcc: victim@example.com"):
        try:
            send(mail_config(smtp.port), title=title)
        except outbound.OutboundError as error:
            assert error.code == "outbound.invalid_mail"
    assert all("Bcc:" not in m.split("\n\n")[0] for m in smtp.messages)


@pytest.mark.parametrize(
    "bad",
    [
        {"recipient": "a@example.com\r\nBcc: b@example.com"},
        {"recipient": "a@example.com, b@example.com"},
        {"recipient": "<a@example.com>"},
        {"sender": "x y@example.com"},
        {"host": "evil.example.com/path"},
        {"port": 0},
        {"security": "tls13"},
        {"username": "user", "security": "none", "password": "pw"},
    ],
)
def test_odd_mail_settings_are_refused_when_the_channel_is_made(bad):
    config = {
        "host": "mail.example.com",
        "port": 587,
        "security": "starttls",
        "sender": "a@example.com",
        "recipient": "b@example.com",
    }
    delivery.EmailConfig.model_validate(config)
    with pytest.raises(ValueError):
        delivery.EmailConfig.model_validate({**config, **bad})


def test_a_password_is_never_sent_without_encryption(smtp):
    with pytest.raises(outbound.OutboundError) as error:
        send(mail_config(smtp.port, username="user", password="secret"))
    assert error.value.code == "outbound.invalid_mail" and smtp.commands == []


def test_starttls_is_required_not_optional(smtp):
    # The server does not offer STARTTLS: we do not fall back to clear text.
    with pytest.raises(outbound.OutboundError) as error:
        send(mail_config(smtp.port, security="starttls"))
    assert error.value.code == "outbound.failed" and smtp.messages == []


def self_signed(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - dt.timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def test_an_untrusted_certificate_is_refused_over_ssl(tmp_path):
    server = SmtpServer(self_signed(tmp_path))
    try:
        with pytest.raises(outbound.OutboundError) as error:
            send(mail_config(server.port, security="ssl"))
        assert error.value.code == "outbound.failed" and server.messages == []
    finally:
        server.close()


def test_private_mail_servers_are_refused_by_default(smtp, monkeypatch):
    monkeypatch.setattr(outbound, "MAIL_PORTS", frozenset({smtp.port}))
    with pytest.raises(outbound.OutboundError) as error:
        send(mail_config(smtp.port), allow_private=False)
    assert error.value.code == "outbound.address_not_allowed" and smtp.commands == []


def test_an_unreachable_server_is_a_plain_failure_without_details():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(outbound.OutboundError) as error:
        send(mail_config(port))
    assert error.value.code == "outbound.failed"


# --- quiet hours ------------------------------------------------------------------------------


def at(hour, minute=0):
    return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)


def setting(**extra):
    return {"enabled": True, "start": "22:00", "end": "07:00", "timezone": "UTC", **extra}


def test_quiet_hours_cross_midnight():
    s = setting()
    assert quiet.is_quiet(s, at(23)) and quiet.is_quiet(s, at(3)) and quiet.is_quiet(s, at(22, 0))
    assert (
        not quiet.is_quiet(s, at(7, 0))
        and not quiet.is_quiet(s, at(12))
        and not quiet.is_quiet(s, at(21, 59))
    )


def test_quiet_hours_within_a_day_and_in_a_time_zone():
    assert quiet.is_quiet(setting(start="13:00", end="14:00"), at(13, 30))
    assert not quiet.is_quiet(setting(start="13:00", end="14:00"), at(14, 0))
    # 23:00 UTC is midnight in Paris in winter: inside 22:00-07:00 local, outside 01:00-06:00.
    assert quiet.is_quiet(setting(timezone="Europe/Paris", start="00:00", end="06:00"), at(23))
    assert not quiet.is_quiet(setting(timezone="Europe/Paris", start="01:00", end="06:00"), at(23))


def test_a_broken_or_disabled_setting_silences_nothing():
    assert not quiet.is_quiet(setting(enabled=False), at(23))
    assert not quiet.is_quiet(setting(start="99:99"), at(23))
    assert not quiet.is_quiet(setting(timezone="Mars/Base"), at(23))
    assert not quiet.is_quiet(setting(start="08:00", end="08:00"), at(8))


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "ab:cd", "١٢:٠٠", "", None, 5])
def test_times_must_be_hh_mm(value):
    with pytest.raises(ValueError):
        quiet.parse(value)


@pytest.fixture
def box(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                encryption_key=generate_key(),
                allow_private_outbound=True,
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
    return client, app, {"Authorization": f"Bearer {token}"}


def confirm_mailbox(client, admin, smtp, channel_id):
    """Read the code from the message the 'mailbox' got, enter it, and forget that message."""
    code = re.search(r"code is (\d{6})", smtp.messages[-1]).group(1)
    got = client.post(
        f"/api/v1/notifications/channels/{channel_id}/confirm", headers=admin, json={"code": code}
    )
    assert got.status_code == 200
    smtp.messages.clear()


def test_the_quiet_hours_api_validates_and_stores_per_account(box):
    client, app, admin = box
    assert client.get("/api/v1/notifications/quiet", headers=admin).json()["enabled"] is False
    good = {"enabled": True, "start": "22:30", "end": "06:45", "timezone": "Europe/Paris"}
    assert client.put("/api/v1/notifications/quiet", headers=admin, json=good).json() == good
    for bad in ({"start": "25:00"}, {"timezone": "../../etc"}, {"end": "6:45"}):
        got = client.put("/api/v1/notifications/quiet", headers=admin, json={**good, **bad})
        assert (
            got.status_code == 422 and got.json()["error"]["code"] == "notifications.invalid_quiet"
        )
    assert (
        client.put(
            "/api/v1/notifications/quiet", headers=admin, json={**good, "extra": 1}
        ).status_code
        == 422
    )
    assert client.put("/api/v1/notifications/quiet", json=good).status_code == 401


def test_quiet_hours_hold_back_channels_but_not_the_inbox_or_critical_events(
    box, smtp, monkeypatch
):
    client, app, admin = box
    from armadok.core.events import emit
    from armadok.modules.notifications.service import wait_for_deliveries

    channel = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "email",
            "name": "mail",
            "settings": {k: v for k, v in mail_config(smtp.port).items() if k != "kind"},
        },
    )
    assert channel.status_code == 201
    channel_id = channel.json()["id"]
    confirm_mailbox(client, admin, smtp, channel_id)
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    rules = [
        {"event": e, "target": t, "min_severity": "info", "node_id": None, "enabled": True}
        for e in ("worker.disconnected", "uptime.down")
        for t in (channel_id, "in_app")
    ]
    client.put("/api/v1/notifications/rules", headers=admin, json={"rules": rules})
    everything = {"enabled": True, "start": "00:00", "end": "23:59", "timezone": "UTC"}
    monkeypatch.setattr(
        quiet, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: at(12))})
    )
    client.put("/api/v1/notifications/quiet", headers=admin, json=everything)

    async def go():
        await emit(
            app, "notify", event="worker.disconnected", node_id=node, params={"node": "home"}
        )
        await emit(
            app,
            "notify",
            event="uptime.down",
            node_id=node,
            params={"node": "home", "name": "blog", "reason": "timeout"},
        )
        await wait_for_deliveries(app)

    asyncio.run(go())
    entries = client.get("/api/v1/notifications", headers=admin).json()["entries"]
    assert {e["event"] for e in entries} == {"worker.disconnected", "uptime.down"}  # inbox: both
    # By e-mail, only the critical one got through the quiet hours.
    assert len(smtp.messages) == 1 and "blog" in smtp.messages[0]


# --- review points ----------------------------------------------------------------------------


def hostile_server(kind, port_holder):
    """A server that never lets the conversation end: it floods replies, or drips them."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port_holder.append(srv.getsockname()[1])

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=talk, args=(conn,), daemon=True).start()

    def talk(conn):
        try:
            if kind == "flood":
                while True:
                    conn.sendall(b"250-" + b"a" * 4000 + b"\r\n" * 1)
            else:
                while True:
                    conn.sendall(b"2")
                    import time

                    time.sleep(0.2)
        except OSError:
            return

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.parametrize("kind", ["flood", "drip"])
def test_a_hostile_server_cannot_hold_a_thread_or_fill_the_memory(kind, monkeypatch):
    import time

    monkeypatch.setattr(outbound, "MAIL_DEADLINE_SECONDS", 1)
    port = []
    srv = hostile_server(kind, port)
    try:
        for _ in range(6):  # more than the 4 threads of the pool: none may stay stuck
            started = time.monotonic()
            with pytest.raises(outbound.OutboundError) as error:
                send(mail_config(port[0]))
            assert error.value.code == "outbound.failed" and time.monotonic() - started < 4
    finally:
        srv.close()


def test_only_mail_ports_are_used_outside_a_private_network(smtp):
    with pytest.raises(outbound.OutboundError) as error:
        send(mail_config(smtp.port), allow_private=False)
    assert error.value.code == "outbound.port_not_allowed" and smtp.commands == []
    outbound.check_mail_port(587, False)
    outbound.check_mail_port(smtp.port, True)


@pytest.mark.parametrize(
    "address",
    [
        "a|b@example.com",
        "a`b@example.com",
        "a%b@example.com",
        "a/b@example.com",
        ".a@example.com",
        "a..b@example.com",
        "a.@example.com",
        "a!b@example.com",
    ],
)
def test_risky_characters_are_not_accepted_in_an_address(address):
    config = {
        "host": "mail.example.com",
        "port": 587,
        "sender": "a@example.com",
        "recipient": "b@example.com",
    }
    with pytest.raises(ValueError):
        delivery.EmailConfig.model_validate({**config, "recipient": address})
    delivery.EmailConfig.model_validate({**config, "recipient": "first.last+tag@example.com"})


def new_mailbox(client, admin, smtp, **extra):
    settings = {k: v for k, v in mail_config(smtp.port).items() if k != "kind"}
    return client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "email", "name": "mail", "settings": {**settings, **extra}},
    )


def test_a_mailbox_is_unused_until_its_owner_enters_the_code(box, smtp):
    client, app, admin = box
    made = new_mailbox(client, admin, smtp)
    assert made.status_code == 201 and made.json()["needs_confirmation"] is True
    channel_id = made.json()["id"]
    assert len(smtp.messages) == 1 and "confirmation code" in smtp.messages[0]
    listed = client.get("/api/v1/notifications/channels", headers=admin).json()
    assert listed[0]["confirmed"] is False
    url = f"/api/v1/notifications/channels/{channel_id}"
    assert (
        client.post(f"{url}/test", headers=admin).json()["error"]["code"]
        == "notifications.unconfirmed"
    )
    wrong = client.post(f"{url}/confirm", headers=admin, json={"code": "000000"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "notifications.wrong_code"
    confirm_mailbox(client, admin, smtp, channel_id)
    assert (
        client.get("/api/v1/notifications/channels", headers=admin).json()[0]["confirmed"] is True
    )
    assert client.post(f"{url}/test", headers=admin).status_code == 200


def test_the_code_cannot_be_guessed_and_expires(box, smtp):
    client, app, admin = box
    channel_id = new_mailbox(client, admin, smtp).json()["id"]
    url = f"/api/v1/notifications/channels/{channel_id}/confirm"
    right = re.search(r"code is (\d{6})", smtp.messages[-1]).group(1)
    for _ in range(5):
        assert (
            client.post(
                url, headers=admin, json={"code": "999999" if right != "999999" else "111111"}
            ).status_code
            == 422
        )
    locked = client.post(url, headers=admin, json={"code": right})
    assert (
        locked.status_code == 429 and locked.json()["error"]["code"] == "notifications.code_locked"
    )
    # An expired code is refused too.
    other = new_mailbox(client, admin, smtp).json()["id"]
    code = re.search(r"code is (\d{6})", smtp.messages[-1]).group(1)
    conn = app.state.settings.open_db()
    conn.execute(
        "UPDATE notification_channels SET confirm_expires = '2000-01-01T00:00:00+00:00'"
        " WHERE id = ?",
        (other,),
    )
    conn.commit()
    conn.close()
    late = client.post(
        f"/api/v1/notifications/channels/{other}/confirm", headers=admin, json={"code": code}
    )
    assert late.json()["error"]["code"] == "notifications.code_expired"


def test_another_account_cannot_confirm_or_see_the_code(box, smtp):
    client, app, admin = box
    channel_id = new_mailbox(client, admin, smtp).json()["id"]
    client.post("/api/v1/users", headers=admin, json={"username": "eve", "password": PASSWORD})
    token = client.post(
        "/api/v1/auth/login", json={"username": "eve", "password": PASSWORD}
    ).json()["access_token"]
    eve = {"Authorization": f"Bearer {token}"}
    code = re.search(r"code is (\d{6})", smtp.messages[-1]).group(1)
    assert (
        client.post(
            f"/api/v1/notifications/channels/{channel_id}/confirm", headers=eve, json={"code": code}
        ).status_code
        == 404
    )


def test_a_mailbox_that_cannot_be_reached_is_not_created(box):
    client, app, admin = box
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    got = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "email",
            "name": "mail",
            "settings": {k: v for k, v in mail_config(port).items() if k != "kind"},
        },
    )
    assert got.status_code == 502
    assert client.get("/api/v1/notifications/channels", headers=admin).json() == []


def test_an_unconfirmed_mailbox_gets_no_event_mail(box, smtp):
    client, app, admin = box
    from armadok.core.events import emit
    from armadok.modules.notifications.service import wait_for_deliveries

    channel_id = new_mailbox(client, admin, smtp).json()["id"]
    smtp.messages.clear()
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    rules = [
        {
            "event": "worker.disconnected",
            "target": channel_id,
            "min_severity": "info",
            "node_id": None,
            "enabled": True,
        }
    ]
    client.put("/api/v1/notifications/rules", headers=admin, json={"rules": rules})

    async def go():
        await emit(
            app, "notify", event="worker.disconnected", node_id=node, params={"node": "home"}
        )
        await wait_for_deliveries(app)

    asyncio.run(go())
    assert smtp.messages == []


def test_quiet_hours_never_lose_an_event_whose_only_rule_is_a_channel(box, smtp, monkeypatch):
    client, app, admin = box
    from armadok.core.events import emit
    from armadok.modules.notifications.service import wait_for_deliveries

    channel_id = new_mailbox(client, admin, smtp).json()["id"]
    confirm_mailbox(client, admin, smtp, channel_id)
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "home", "type": "docker"}
    ).json()["id"]
    rules = [
        {
            "event": "worker.disconnected",
            "target": channel_id,
            "min_severity": "info",
            "node_id": None,
            "enabled": True,
        }
    ]
    client.put("/api/v1/notifications/rules", headers=admin, json={"rules": rules})
    monkeypatch.setattr(
        quiet, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: at(12))})
    )
    client.put(
        "/api/v1/notifications/quiet",
        headers=admin,
        json={"enabled": True, "start": "00:00", "end": "23:59", "timezone": "UTC"},
    )

    async def go():
        await emit(
            app, "notify", event="worker.disconnected", node_id=node, params={"node": "home"}
        )
        await wait_for_deliveries(app)

    asyncio.run(go())
    assert smtp.messages == []  # held back
    entries = client.get("/api/v1/notifications", headers=admin).json()["entries"]
    assert [e["event"] for e in entries] == ["worker.disconnected"]  # but not lost


def test_a_mailbox_cannot_be_flooded_through_a_rule(box, smtp):
    client, app, admin = box
    from armadok.modules.notifications import service

    state = {}
    assert all(service._within_rate(state, "c", "email") for _ in range(20))
    assert service._within_rate(state, "c", "email") is False
    assert service._within_rate(state, "other", "email") is True  # per channel
    assert all(service._within_rate(state, "w", "webhook") for _ in range(120))
    assert service._within_rate(state, "w", "webhook") is False


def test_channel_and_quiet_changes_are_audited_without_the_settings(box, smtp):
    client, app, admin = box
    channel_id = new_mailbox(client, admin, smtp, password="").json()["id"]
    confirm_mailbox(client, admin, smtp, channel_id)
    client.post(f"/api/v1/notifications/channels/{channel_id}/test", headers=admin)
    client.put(
        "/api/v1/notifications/quiet",
        headers=admin,
        json={"enabled": True, "start": "22:00", "end": "07:00", "timezone": "UTC"},
    )
    client.delete(f"/api/v1/notifications/channels/{channel_id}", headers=admin)
    log = client.get("/api/v1/audit", headers=admin).text
    for event in (
        "channel_create",
        "channel_confirm",
        "channel_test",
        "quiet_set",
        "channel_delete",
    ):
        assert f"notification.{event}" in log
    assert "me@example.com" not in log and "127.0.0.1" not in log


def test_the_default_quiet_hours_do_not_pretend_to_have_a_time_zone(box):
    client, app, admin = box
    assert client.get("/api/v1/notifications/quiet", headers=admin).json()["timezone"] == ""


def test_an_administrator_can_turn_the_mail_channel_off_for_everybody(box, smtp):
    client, app, admin = box
    url = "/api/v1/notifications/platform"
    assert client.get(url, headers=admin).json() == {"email_enabled": True, "can_change": True}
    # A person without the right can read the state but not change it.
    client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    assert client.get(url, headers=bob).json()["can_change"] is False
    assert client.put(url, headers=bob, json={"email_enabled": False}).status_code == 403
    # Turned off: no new mailbox, and an existing confirmed one is not written to.
    made = new_mailbox(client, admin, smtp)
    assert made.status_code == 201
    assert client.put(url, headers=admin, json={"email_enabled": False}).status_code == 200
    assert new_mailbox(client, admin, smtp).status_code == 409
    before = len(smtp.messages)
    test = client.post(f"/api/v1/notifications/channels/{made.json()['id']}/test", headers=admin)
    assert test.status_code == 409 and len(smtp.messages) == before
    assert client.put(url, headers=admin, json={"email_enabled": True}).status_code == 200
    assert new_mailbox(client, admin, smtp).status_code == 201
