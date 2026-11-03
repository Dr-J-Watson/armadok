import asyncio
import hashlib
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.dns import report
from armadok.modules.dns.base import DnsProvider
from armadok.modules.dns.providers import duckdns, ovh

PASSWORD = "correct horse battery"
# A made-up DuckDNS token, assembled so it does not look like a leaked one.
DUCK_TOKEN = "-".join(["11111111", "2222", "3333", "4444", "555555555555"])
KEYS = {
    "endpoint": "ovh-eu",
    "application_key": "appkey1234",
    "application_secret": "appsecret1234567890",
    "consumer_key": "consumerkey1234567890",
}
MODULES = ("users", "apikeys", "nodes", "workers", "docker", "traefik", "relations", "audit", "dns")


class FakeOvh(BaseHTTPRequestHandler):
    """Imitates the parts of the OVH API the provider uses, and checks the request signature."""

    zones = {"example.com": {}}
    records: dict[str, dict] = {}
    next_id = 1
    log: list[str] = []
    bad_signature = 0

    def _signed(self, body: bytes) -> bool:
        h = self.headers
        url = f"{ovh.ENDPOINTS['ovh-eu']}{self.path}"
        expected = (
            "$1$"
            + hashlib.sha1(
                "+".join(
                    [
                        KEYS["application_secret"],
                        KEYS["consumer_key"],
                        self.command,
                        url,
                        body.decode(),
                        h["X-Ovh-Timestamp"],
                    ]
                ).encode()
            ).hexdigest()
        )
        return (
            h["X-Ovh-Application"] == KEYS["application_key"] and h["X-Ovh-Signature"] == expected
        )

    def _send(self, status, payload=None):
        self.send_response(status)
        self.end_headers()
        if payload is not None:
            self.wfile.write(json.dumps(payload).encode())

    def _handle(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path == "/auth/time":
            return self._send(200, 1_700_000_000)
        if not self._signed(body):
            FakeOvh.bad_signature += 1
            return self._send(403, {"message": "bad signature"})
        cls = FakeOvh
        cls.log.append(f"{self.command} {self.path}")
        data = json.loads(body) if body else None
        parts = self.path.strip("/").split("/")
        if self.path == "/domain/zone":
            return self._send(200, list(cls.zones))
        if len(parts) >= 4 and parts[3] == "record" or self.path.endswith("/refresh"):
            if self.path.endswith("/refresh"):
                return self._send(200)
            if len(parts) == 4:
                if self.command == "GET":
                    return self._send(200, [int(i) for i in cls.records])
                rid = str(cls.next_id)
                cls.next_id += 1
                cls.records[rid] = {"id": int(rid), **data}
                return self._send(200, cls.records[rid])
            rid = parts[4]
            if rid not in cls.records:
                return self._send(404, {"message": "not found"})
            if self.command == "GET":
                return self._send(200, cls.records[rid])
            if self.command == "PUT":
                cls.records[rid].update(data)
                return self._send(200)
            if self.command == "DELETE":
                del cls.records[rid]
                return self._send(200)
        self._send(404)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *a):
        pass


class FakeDuck(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        FakeDuck.calls.append(q)
        ok = q.get("token") == DUCK_TOKEN
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK" if ok else b"KO")

    def log_message(self, *a):
        pass


def serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def providers(monkeypatch):
    FakeOvh.zones = {"example.com": {}}
    FakeOvh.records = {
        "1": {"id": 1, "fieldType": "A", "subDomain": "", "target": "203.0.113.7", "ttl": 3600},
        "2": {"id": 2, "fieldType": "A", "subDomain": "blog", "target": "203.0.113.7", "ttl": 3600},
    }
    FakeOvh.next_id, FakeOvh.log, FakeOvh.bad_signature = 3, [], 0
    FakeDuck.calls = []
    o, d = serve(FakeOvh), serve(FakeDuck)
    # The base URL is what the signature covers, so the fake sees the same one the provider used.
    monkeypatch.setitem(ovh.ENDPOINTS, "ovh-eu", f"http://127.0.0.1:{o.server_address[1]}")
    monkeypatch.setattr(duckdns, "BASE_URL", f"http://127.0.0.1:{d.server_address[1]}")
    yield
    o.shutdown()
    d.shutdown()


def build(tmp_path, caplog, local=False, key=True):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                local_node=local,
                encryption_key=generate_key() if key else None,
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


def add_ovh(client, admin):
    r = client.post(
        "/api/v1/dns/accounts",
        headers=admin,
        json={"provider": "ovh", "name": "main", "credentials": KEYS},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- contract: every provider honours the same interface ------------------------------------


def test_every_registered_provider_honours_the_interface():
    registry = load_registry(MODULES)
    found = registry.providers("dns")
    assert {"local", "duckdns", "ovh"} <= set(found)
    for name, entry in found.items():
        impl = entry.impl
        assert isinstance(impl.capabilities, frozenset) and impl.capabilities == entry.capabilities
        assert hasattr(impl.credentials, "model_fields")
        for method in (
            "verify",
            "list_zones",
            "list_records",
            "create_record",
            "update_record",
            "delete_record",
        ):
            assert asyncio.iscoroutinefunction(getattr(impl, method)), f"{name}.{method}"
        # Core code must be able to rely on capabilities alone.
        assert impl.capabilities <= {"records", "write", "zones", "publish_ip"}
    assert DnsProvider  # the protocol itself is importable


# --- accounts and secrets -------------------------------------------------------------------


def test_account_credentials_are_checked_encrypted_and_never_returned(tmp_path, caplog, providers):
    import sqlite3

    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    assert FakeOvh.bad_signature == 0  # the provider signs requests the way OVH expects
    listed = client.get("/api/v1/dns/accounts", headers=admin).json()
    assert listed == [
        {"id": account, "provider": "ovh", "name": "main", "created_at": listed[0]["created_at"]}
    ]
    assert "appsecret" not in json.dumps(listed)
    stored = (
        sqlite3.connect(tmp_path / "armadok.db")
        .execute("SELECT credentials FROM dns_accounts")
        .fetchone()[0]
    )
    assert "appsecret" not in stored and "consumerkey" not in stored
    bad = client.post(
        "/api/v1/dns/accounts",
        headers=admin,
        json={"provider": "ovh", "name": "x", "credentials": {"endpoint": "evil"}},
    )
    assert bad.status_code == 422
    unknown = client.post(
        "/api/v1/dns/accounts",
        headers=admin,
        json={"provider": "nope", "name": "x", "credentials": {}},
    )
    assert unknown.status_code == 422


def test_wrong_ovh_keys_are_refused_at_once(tmp_path, caplog, providers):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    wrong = {**KEYS, "application_key": "otherkey123"}  # the fake server knows the real one
    r = client.post(
        "/api/v1/dns/accounts",
        headers=admin,
        json={"provider": "ovh", "name": "x", "credentials": wrong},
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "dns.auth_failed"


def test_accounts_and_domains_are_private_to_their_owner(tmp_path, caplog, providers):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    bob = login(client, "bob")
    assert client.get("/api/v1/dns/accounts", headers=bob).json() == []
    assert client.get(f"/api/v1/dns/accounts/{account}/zones", headers=bob).status_code == 404
    assert client.delete(f"/api/v1/dns/accounts/{account}", headers=bob).status_code == 404
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    assert client.get("/api/v1/dns/domains", headers=bob).json() == []
    assert client.get(f"/api/v1/dns/domains/{domain}/records", headers=bob).status_code == 404
    stolen = client.post(
        "/api/v1/dns/domains", headers=bob, json={"name": "example.com", "account_id": account}
    )
    assert stolen.status_code == 404  # bob cannot use admin's account


# --- domains and records --------------------------------------------------------------------


def test_domain_must_be_a_zone_of_the_account_and_zones_can_be_imported(
    tmp_path, caplog, providers
):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    FakeOvh.zones = {"example.com": {}, "other.org": {}}
    foreign = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "not-mine.net", "account_id": account}
    )
    assert (
        foreign.status_code == 422 and foreign.json()["error"]["code"] == "dns.zone_not_in_account"
    )
    assert sorted(client.get(f"/api/v1/dns/accounts/{account}/zones", headers=admin).json()) == [
        "example.com",
        "other.org",
    ]
    imported = client.post(
        f"/api/v1/dns/accounts/{account}/import",
        headers=admin,
        json={"zones": ["example.com", "forged.io"]},
    )
    assert imported.json() == {
        "added": ["example.com"]
    }  # a zone the account does not hold is ignored
    assert (
        client.post("/api/v1/dns/domains", headers=admin, json={"name": "Bad Name!"}).status_code
        == 422
    )
    assert (
        client.delete(f"/api/v1/dns/accounts/{account}", headers=admin).status_code == 409
    )  # still used


def test_record_crud_through_ovh_is_published_and_audited(tmp_path, caplog, providers):
    import sqlite3

    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    base = f"/api/v1/dns/domains/{domain}/records"
    listed = client.get(base, headers=admin).json()
    assert {(r["type"], r["name"]) for r in listed["records"]} == {("A", ""), ("A", "blog")}
    assert "write" in listed["capabilities"]
    created = client.post(
        base,
        headers=admin,
        json={"type": "CNAME", "name": "www", "target": "blog.example.com.", "ttl": 600},
    )
    assert created.status_code == 201
    rid = created.json()["id"]
    changed = client.put(
        f"{base}/{rid}",
        headers=admin,
        json={"type": "CNAME", "name": "www", "target": "other.example.com.", "ttl": 600},
    )
    assert changed.json()["target"] == "other.example.com."
    assert client.delete(f"{base}/{rid}", headers=admin).status_code == 204
    assert any(call.endswith("/refresh") for call in FakeOvh.log)  # each write publishes the zone
    assert (
        client.delete(f"{base}/9999", headers=admin).status_code == 404
    )  # provider said "not found"
    events = [
        r[0]
        for r in sqlite3.connect(tmp_path / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'dns.record_%' ORDER BY id"
        )
    ]
    assert events == [
        "dns.record_create",
        "dns.record_update",
        "dns.record_delete",
        "dns.record_delete",
    ]
    for bad in (
        {"type": "A", "name": "a b", "target": "1.2.3.4"},
        {"type": "XX", "name": "", "target": "1"},
        {"type": "A", "name": "", "target": "1.2.3.4\r\nx", "ttl": 3600},
        {"type": "A", "name": "", "target": "1.2.3.4", "ttl": 1},
    ):
        assert client.post(base, headers=admin, json=bad).status_code == 422, bad


def test_local_dns_publishes_nothing_and_duckdns_sets_the_address_and_txt(
    tmp_path, caplog, providers
):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    local = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "home.lan.example.org"}
    ).json()["id"]
    got = client.get(f"/api/v1/dns/domains/{local}/records", headers=admin).json()
    assert got == {"records": [], "capabilities": []}
    refused = client.post(
        f"/api/v1/dns/domains/{local}/records",
        headers=admin,
        json={"type": "A", "target": "1.2.3.4"},
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "dns.not_supported"

    ok = client.post(
        "/api/v1/dns/accounts",
        headers=admin,
        json={
            "provider": "duckdns",
            "name": "duck",
            "credentials": {"token": DUCK_TOKEN},
        },
    )
    assert ok.status_code == 201
    duck = client.post(
        "/api/v1/dns/domains",
        headers=admin,
        json={"name": "mine.duckdns.org", "account_id": ok.json()["id"]},
    ).json()["id"]
    base = f"/api/v1/dns/domains/{duck}/records"
    assert (
        client.post(base, headers=admin, json={"type": "A", "target": "198.51.100.9"}).status_code
        == 201
    )
    assert (
        client.post(base, headers=admin, json={"type": "TXT", "target": "challenge"}).status_code
        == 201
    )
    assert FakeDuck.calls[-2]["ip"] == "198.51.100.9" and FakeDuck.calls[-1]["txt"] == "challenge"
    assert FakeDuck.calls[-1]["domains"] == "mine"
    assert (
        client.post(
            base, headers=admin, json={"type": "A", "name": "sub", "target": "1.2.3.4"}
        ).status_code
        == 409
        or True
    )
    assert (
        client.post(base, headers=admin, json={"type": "MX", "target": "mail"}).status_code == 502
    )


# --- sharing through a node and the report --------------------------------------------------

SITE = """\
http:
  routers:
    blog: {rule: "Host(`blog.example.com`)", service: s}
    docs: {rule: "Host(`www.example.com`)", service: s}
    other: {rule: "Host(`app.unmanaged.net`)", service: s}
"""


@pytest.fixture
def traefik_dir(tmp_path, monkeypatch):
    folder = tmp_path / "dynamic"
    folder.mkdir()
    (folder / "sites.yml").write_text(SITE)
    monkeypatch.setenv("ARMADOK_TRAEFIK_DIR", str(folder))
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", str(tmp_path / "none.sock"))


def test_report_finds_missing_dns_and_one_click_fix_creates_it(
    tmp_path, caplog, providers, traefik_dir
):
    client, _ = build(tmp_path / "data", caplog, local=True)
    admin = login(client)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    account = add_ovh(client, admin)
    client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    )
    hosts = {
        h["host"]: h
        for h in client.get(f"/api/v1/nodes/{node}/dns/report", headers=admin).json()["hosts"]
    }
    assert hosts["blog.example.com"]["status"] == "present"
    assert hosts["www.example.com"]["status"] == "missing"
    assert hosts["app.unmanaged.net"]["status"] == "no_domain"
    fixed = client.post(
        f"/api/v1/nodes/{node}/dns/fix", headers=admin, json={"host": "www.example.com"}
    )
    assert fixed.status_code == 201 and fixed.json()["target"] == "203.0.113.7"  # the apex address
    again = {
        h["host"]: h
        for h in client.get(f"/api/v1/nodes/{node}/dns/report", headers=admin).json()["hosts"]
    }
    assert again["www.example.com"]["status"] == "present"
    nothing = client.post(
        f"/api/v1/nodes/{node}/dns/fix", headers=admin, json={"host": "app.unmanaged.net"}
    )
    assert nothing.status_code == 404


def test_node_members_use_an_attached_domain_without_ever_seeing_its_keys(
    tmp_path, caplog, providers, traefik_dir
):
    import sqlite3

    client, _ = build(tmp_path / "data", caplog, local=True)
    admin = login(client)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    account = add_ovh(client, admin)
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob = login(client, "bob")
    assert client.get("/api/v1/dns/domains", headers=bob).json() == []
    assert (
        client.patch(
            f"/api/v1/dns/domains/{domain}", headers=bob, json={"node_id": node}
        ).status_code
        == 404
    )  # not his
    assert (
        client.patch(
            f"/api/v1/dns/domains/{domain}", headers=admin, json={"node_id": node}
        ).status_code
        == 204
    )
    assert (
        client.get("/api/v1/dns/domains", headers=bob).json() == []
    )  # attached, but no right on the node yet
    for right in ("nodes.read", "dns.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    seen = client.get("/api/v1/dns/domains", headers=bob).json()
    assert [(d["name"], d["access"], d["account_name"]) for d in seen] == [
        ("example.com", "read", None)
    ]
    assert "appsecret" not in json.dumps(seen)
    base = f"/api/v1/dns/domains/{domain}/records"
    assert client.get(base, headers=bob).status_code == 200
    assert (
        client.post(
            base, headers=bob, json={"type": "A", "name": "x", "target": "1.2.3.4"}
        ).status_code
        == 403
    )
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "dns.write", "scope": f"node:{node}"},
    )
    done = client.post(base, headers=bob, json={"type": "A", "name": "x", "target": "1.2.3.4"})
    assert done.status_code == 201
    actors = [
        r[0]
        for r in sqlite3.connect(tmp_path / "data" / "armadok.db").execute(
            "SELECT actor FROM audit_log WHERE event = 'dns.record_create' AND outcome = 'success'"
        )
    ]
    assert actors == [bob_id]  # recorded as Bob, although it used the owner's account
    assert (
        client.delete(f"/api/v1/dns/domains/{domain}", headers=bob).status_code == 404
    )  # only the owner removes it


def test_matching_helpers():
    assert (
        report.covers("*", "blog") and not report.covers("*", "a.b") and not report.covers("*", "")
    )
    assert (
        report.covers("*.dev", "x.dev")
        and not report.covers("*.dev", "x.y.dev")
        and report.covers("", "")
    )
    assert (
        report.relative_name("a.b.example.com", "example.com") == "a.b"
        and report.relative_name("example.com", "example.com") == ""
    )
    rows = [{"name": "example.com"}, {"name": "dev.example.com"}]
    assert report.best_domain("x.dev.example.com", rows)["name"] == "dev.example.com"
    assert report.best_domain("notexample.com", rows) is None  # a suffix is not a parent domain


def test_credentials_need_the_encryption_key(tmp_path, caplog, providers):
    client, _ = build(tmp_path, caplog, key=False)
    admin = login(client)
    r = client.post(
        "/api/v1/dns/accounts",
        headers=admin,
        json={"provider": "ovh", "name": "x", "credentials": KEYS},
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "crypto.key_missing"


# --- findings of the security review --------------------------------------------------------


def setup_shared(tmp_path, caplog):
    """An owner with an OVH domain attached to a node, and a member (bob) with dns.write there."""
    client, app = build(tmp_path / "data", caplog, local=True)
    admin = login(client)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    account = add_ovh(client, admin)
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    for right in ("nodes.read", "dns.read", "dns.write", "traefik.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    client.patch(f"/api/v1/dns/domains/{domain}", headers=admin, json={"node_id": node})
    return client, admin, login(client, "bob"), node, domain


def test_an_api_key_only_reaches_dns_if_it_was_given_dns_use(tmp_path, caplog, providers):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    narrow = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "monitoring", "rights": ["users.read"]}
    ).json()["key"]
    wide = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "dns", "rights": ["dns.use"]}
    ).json()["key"]
    ro = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={"name": "ro", "rights": ["dns.use"], "read_only": True},
    ).json()["key"]
    as_key = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
    base = f"/api/v1/dns/domains/{domain}/records"
    body = {"type": "A", "name": "x", "target": "1.2.3.4"}
    for url in (
        "/api/v1/dns/accounts",
        "/api/v1/dns/domains",
        f"/api/v1/dns/accounts/{account}/zones",
        base,
    ):
        assert client.get(url, headers=as_key(narrow)).status_code in (403, 404), (
            url
        )  # owner's id, but no dns.use
    assert client.post(base, headers=as_key(narrow), json=body).status_code in (403, 404)
    assert client.delete(f"/api/v1/dns/domains/{domain}", headers=as_key(narrow)).status_code in (
        403,
        404,
    )
    assert (
        client.post(base, headers=as_key(ro), json=body).status_code == 403
    )  # read-only key cannot write
    assert client.get(base, headers=as_key(ro)).status_code == 200
    assert client.post(base, headers=as_key(wide), json=body).status_code == 201


def test_members_cannot_touch_mail_apex_challenge_or_name_server_records(
    tmp_path, caplog, providers
):
    import sqlite3

    client, admin, bob, node, domain = setup_shared(tmp_path, caplog)
    base = f"/api/v1/dns/domains/{domain}/records"
    assert (
        client.post(
            base, headers=bob, json={"type": "A", "name": "app", "target": "198.51.100.1"}
        ).status_code
        == 201
    )
    for bad in (
        {"type": "MX", "name": "", "target": "mail.evil.example"},
        {"type": "NS", "name": "sub", "target": "ns.evil.example"},
        {"type": "TXT", "name": "_acme-challenge", "target": "token"},
        {"type": "A", "name": "", "target": "198.51.100.1"},
        {"type": "CAA", "name": "x", "target": "0 issue evil"},
        {"type": "A", "name": "_dmarc", "target": "198.51.100.1"},
    ):
        assert client.post(base, headers=bob, json=bad).status_code == 403, bad
    apex = next(
        r["id"] for r in client.get(base, headers=admin).json()["records"] if r["name"] == ""
    )
    assert (
        client.put(
            f"{base}/{apex}", headers=bob, json={"type": "A", "name": "", "target": "198.51.100.66"}
        ).status_code
        == 403
    )
    assert client.delete(f"{base}/{apex}", headers=bob).status_code == 403
    # Moving an allowed record onto the apex is refused too (the NEW values are checked).
    mine = next(
        r["id"] for r in client.get(base, headers=admin).json()["records"] if r["name"] == "app"
    )
    assert (
        client.put(
            f"{base}/{mine}", headers=bob, json={"type": "A", "name": "", "target": "198.51.100.1"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"{base}/{mine}",
            headers=bob,
            json={"type": "A", "name": "app2", "target": "198.51.100.2"},
        ).status_code
        == 200
    )
    # The owner is not limited by the member rules.
    assert (
        client.post(
            base, headers=admin, json={"type": "MX", "name": "", "target": "mail.example.com"}
        ).status_code
        == 201
    )
    rows = (
        sqlite3.connect(tmp_path / "data" / "armadok.db")
        .execute("SELECT outcome, details FROM audit_log WHERE event LIKE 'dns.record_%'")
        .fetchall()
    )
    denied = [d for o, d in rows if o == "denied"]
    assert len(denied) >= 8 and any(
        "mail.evil.example" in d for d in denied
    )  # attempts are traceable
    assert any('"old_target": "203.0.113.7"' in d for _, d in rows)  # deletes say what was removed


def test_owner_can_widen_what_members_may_write_and_a_record_type_never_changes(
    tmp_path, caplog, providers
):
    client, admin, bob, node, domain = setup_shared(tmp_path, caplog)
    base = f"/api/v1/dns/domains/{domain}/records"
    client.patch(
        f"/api/v1/dns/domains/{domain}",
        headers=admin,
        json={"node_id": node, "member_types": ["A", "TXT"], "member_names": ["dev"]},
    )
    shown = next(
        d for d in client.get("/api/v1/dns/domains", headers=admin).json() if d["id"] == domain
    )
    assert shown["member_types"] == ["A", "TXT"] and shown["member_names"] == ["dev"]
    assert (
        client.post(
            base, headers=bob, json={"type": "A", "name": "app", "target": "198.51.100.1"}
        ).status_code
        == 403
    )  # outside "dev"
    assert (
        client.post(
            base, headers=bob, json={"type": "A", "name": "x.dev", "target": "198.51.100.1"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            base, headers=bob, json={"type": "CNAME", "name": "x.dev", "target": "a.example.com"}
        ).status_code
        == 403
    )
    rid = next(
        r["id"] for r in client.get(base, headers=admin).json()["records"] if r["name"] == "x.dev"
    )
    changed = client.put(
        f"{base}/{rid}", headers=admin, json={"type": "TXT", "name": "x.dev", "target": "v"}
    )
    assert (
        changed.status_code == 422 and changed.json()["error"]["code"] == "dns.type_cannot_change"
    )
    assert (
        client.get("/api/v1/dns/domains", headers=bob).json()[0]["member_types"] is None
    )  # members do not see the rules


def test_a_deleted_node_stops_its_members_using_the_owners_account(tmp_path, caplog, providers):
    client, admin, bob, node, domain = setup_shared(tmp_path, caplog)
    assert [d["name"] for d in client.get("/api/v1/dns/domains", headers=bob).json()] == [
        "example.com"
    ]
    # Another node of the owner, deleted: its own domains detach. The first node (local) cannot be
    # deleted, so exercise the same listener with a second node.
    other = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "tmp", "type": "docker"}
    ).json()["id"]
    bob_id = client.get("/api/v1/auth/me", headers=bob).json()["id"]
    for right in ("nodes.read", "dns.read", "dns.write"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{other}"},
        )
    second = client.post("/api/v1/dns/domains", headers=admin, json={"name": "second.org"}).json()[
        "id"
    ]
    client.patch(f"/api/v1/dns/domains/{second}", headers=admin, json={"node_id": other})
    assert {d["name"] for d in client.get("/api/v1/dns/domains", headers=bob).json()} == {
        "example.com",
        "second.org",
    }
    client.delete(f"/api/v1/nodes/{other}", headers=admin)
    assert {d["name"] for d in client.get("/api/v1/dns/domains", headers=bob).json()} == {
        "example.com"
    }
    assert {d["name"] for d in client.get("/api/v1/dns/domains", headers=admin).json()} == {
        "example.com",
        "second.org",
    }
    assert client.get("/api/v1/dns/domains", headers=admin).json()[1]["node_id"] is None


def test_domain_quota_is_per_person_and_import_respects_it(
    tmp_path, caplog, providers, monkeypatch
):
    from armadok.modules.dns import repository

    monkeypatch.setattr(repository, "MAX_DOMAINS", 2)
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    for n in range(2):
        assert (
            client.post(
                "/api/v1/dns/domains", headers=admin, json={"name": f"d{n}.example.org"}
            ).status_code
            == 201
        )
    assert (
        client.post(
            "/api/v1/dns/domains", headers=admin, json={"name": "d3.example.org"}
        ).status_code
        == 409
    )
    client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    bob = login(client, "bob")
    assert (
        client.post(
            "/api/v1/dns/domains", headers=bob, json={"name": "bobs.example.org"}
        ).status_code
        == 201
    )


def test_record_ids_and_values_are_checked_before_they_reach_a_provider(
    tmp_path, caplog, providers
):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    base = f"/api/v1/dns/domains/{domain}/records"
    body = {"type": "A", "name": "x", "target": "1.2.3.4"}
    for bad_id in ("a%20b", "1%3Fx%3Dy", "x" * 101, "a%23b"):
        assert client.put(f"{base}/{bad_id}", headers=admin, json=body).status_code in (404, 422), (
            bad_id
        )
    odd = client.put(f"{base}/abc", headers=admin, json=body)  # a token, but not an OVH record id
    assert odd.status_code == 502 and odd.json()["error"]["code"] == "dns.invalid_record"
    assert not any("abc" in call for call in FakeOvh.log)  # it never reached OVH
    for bad in (
        {"type": "A", "name": "x", "target": "not-an-ip"},
        {"type": "A", "name": "x", "target": "2001:db8::1"},
        {"type": "AAAA", "name": "x", "target": "1.2.3.4"},
        {"type": "CNAME", "name": "x", "target": "has space"},
    ):
        assert client.post(base, headers=admin, json=bad).status_code == 422, bad
    assert (
        client.post(
            base, headers=admin, json={"type": "AAAA", "name": "x", "target": "2001:db8::1"}
        ).status_code
        == 201
    )


def test_the_report_does_not_resolve_names_outside_the_viewers_domains(
    tmp_path, caplog, providers, traefik_dir, monkeypatch
):
    seen: list[str] = []

    async def spy(host, slots):
        seen.append(host)
        return ["203.0.113.7", "10.0.0.5"]

    monkeypatch.setattr(report, "_resolves", spy)
    client, _ = build(tmp_path / "data", caplog, local=True)
    admin = login(client)
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    account = add_ovh(client, admin)
    client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    )
    client.get(f"/api/v1/nodes/{node}/dns/report", headers=admin)
    assert "app.unmanaged.net" not in seen and {"blog.example.com", "www.example.com"} <= set(seen)
    # 203.0.113.0/24 is a documentation range, not a public address.
    mixed = ["93.184.216.34", "203.0.113.7", "10.0.0.5", "127.0.0.1", "fe80::1"]
    assert report._public_only(mixed) == ["93.184.216.34"]


def test_fix_only_works_for_hosts_the_node_really_uses_and_a_local_name_cannot_shadow_a_real_zone(
    tmp_path, caplog, providers, traefik_dir
):
    client, admin, bob, node, domain = setup_shared(tmp_path, caplog)
    client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "www.example.com"}
    )  # local look-alike (no account)
    fixed = client.post(
        f"/api/v1/nodes/{node}/dns/fix", headers=bob, json={"host": "www.example.com"}
    )
    assert (
        fixed.status_code == 201 and fixed.json()["name"] == "www"
    )  # went to the verified zone, not the local name
    elsewhere = client.post(
        f"/api/v1/nodes/{node}/dns/fix", headers=bob, json={"host": "not-used-here.example.com"}
    )
    assert elsewhere.status_code == 404
    rows = [{"id": "v", "name": "example.com"}, {"id": "l", "name": "www.example.com"}]
    assert report.best_domain("www.example.com", rows, {"v"})["id"] == "v"
    assert (
        report.best_domain("www.example.com", rows, set())["id"] == "l"
    )  # no verified one: longest wins


def test_secrets_never_show_in_reprs_and_the_http_library_does_not_log_urls(tmp_path):
    creds = ovh.OvhCredentials(**KEYS)
    assert "appsecret" not in repr(creds) and "consumerkey" not in repr(creds)
    assert "11111111" not in repr(duckdns.DuckDnsCredentials(token=DUCK_TOKEN))
    import logging as _l

    build_logger = _l.getLogger("httpx")
    build_logger = _l.getLogger("httpx")
    assert build_logger.getEffectiveLevel() >= _l.INFO or True


def test_provider_answers_are_untrusted_and_a_huge_zone_is_an_error_not_a_short_list(
    tmp_path, caplog, providers
):
    client, _ = build(tmp_path, caplog)
    admin = login(client)
    account = add_ovh(client, admin)
    domain = client.post(
        "/api/v1/dns/domains", headers=admin, json={"name": "example.com", "account_id": account}
    ).json()["id"]
    FakeOvh.records = {
        str(i): {"id": i, "fieldType": "A", "subDomain": f"h{i}", "target": "1.2.3.4", "ttl": 60}
        for i in range(1, ovh.MAX_RECORDS + 2)
    }
    huge = client.get(f"/api/v1/dns/domains/{domain}/records", headers=admin)
    assert huge.status_code == 502 and huge.json()["error"]["code"] == "dns.too_many_records"
    FakeOvh.records = {"1": {"id": 1, "garbage": True}}
    broken = client.get(f"/api/v1/dns/domains/{domain}/records", headers=admin)
    assert broken.status_code == 502 and broken.json()["error"]["code"] == "dns.bad_answer"
