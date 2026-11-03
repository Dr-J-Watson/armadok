import asyncio
import base64
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.certs import schemas, service
from armadok.modules.certs.watch import check_certificates
from armadok.worker import certs_ops
from armadok.worker.operation import OperationError

PASSWORD = "correct horse battery"
KEY_MARKER = "PRIVATE-KEY-MATERIAL-THAT-MUST-NEVER-LEAVE"
MODULES = ("users", "nodes", "workers", "traefik", "relations", "audit", "notifications", "certs")


def make_cert(names, days, issuer="Test CA"):
    """A self-signed certificate valid for `days` more days (negative: already expired)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    issuer_name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, issuer),
            x509.NameAttribute(NameOID.COMMON_NAME, "Test CA R1"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=60))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode()


def acme_file(path, entries, extra=None):
    doc = {
        "letsencrypt": {
            "Account": {"PrivateKey": KEY_MARKER, "Email": "someone@example.com"},
            "Certificates": [
                {"domain": {"main": n[0]}, "certificate": make_cert(n, d), "key": KEY_MARKER}
                for n, d in entries
            ],
        }
    }
    doc.update(extra or {})
    path.write_text(json.dumps(doc))
    path.chmod(0o600)


@pytest.fixture
def acme(tmp_path, monkeypatch):
    folder = tmp_path / "acme"
    folder.mkdir()
    file = folder / "acme.json"
    acme_file(
        file,
        [
            (["blog.example.com"], 80),
            (["*.example.com", "example.com"], 10),
            (["old.example.org"], -3),
        ],
    )
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(file))
    return file


# --- the worker: public facts only, never a key -------------------------------------------------


def test_only_public_facts_are_returned_and_no_key_material_survives(acme):
    out = certs_ops.certs_list({})
    assert KEY_MARKER not in json.dumps(out) and "someone@example.com" not in json.dumps(out)
    by_first = {c["names"][0]: c for c in out["certificates"]}
    assert set(by_first) == {"blog.example.com", "*.example.com", "old.example.org"}
    wildcard = by_first["*.example.com"]
    assert (
        wildcard["names"] == ["*.example.com", "example.com"]
        and wildcard["issuer"] == "Test CA / Test CA R1"
    )
    assert wildcard["resolver"] == "letsencrypt" and re.fullmatch(r"[0-9a-f]+", wildcard["serial"])
    assert set(wildcard) == {
        "resolver",
        "names",
        "issuer",
        "not_before",
        "not_after",
        "serial",
    }  # no field for a key


def test_odd_or_hostile_files_give_plain_codes_that_quote_nothing(tmp_path, monkeypatch):
    def code_of():
        with pytest.raises(OperationError) as caught:
            certs_ops.certs_list({})
        return caught.value.code

    monkeypatch.delenv("ARMADOK_ACME_FILE", raising=False)
    assert code_of() == "worker.acme_not_configured"
    for bad in ("relative/acme.json", "/acme.json", str(tmp_path / "bad name!")):
        monkeypatch.setenv("ARMADOK_ACME_FILE", bad)
        assert code_of() == "worker.acme_not_configured"
    file = tmp_path / "acme.json"
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(file))
    file.write_text(f"not json {KEY_MARKER}")
    assert code_of() == "worker.acme_invalid" and KEY_MARKER not in code_of()
    file.write_text("[1, 2]")
    assert code_of() == "worker.acme_invalid"
    file.write_text(
        json.dumps(
            {
                "r": {
                    "Certificates": [
                        {"certificate": "!!notbase64", "key": KEY_MARKER},
                        5,
                        {"certificate": 7},
                    ]
                }
            }
        )
    )
    assert certs_ops.certs_list({}) == {
        "certificates": [],
        "truncated": False,
    }  # junk entries are skipped
    assert code_of.__name__  # (keeps the helper used)


def test_symlinked_and_oversized_acme_files_are_refused(tmp_path, monkeypatch):
    real = tmp_path / "real.json"
    acme_file(real, [(["a.example.com"], 30)])
    link = tmp_path / "acme.json"
    os.symlink(real, link)
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(link))
    with pytest.raises(OperationError) as caught:
        certs_ops.certs_list({})
    assert caught.value.code == "worker.forbidden"
    link.unlink()
    link.write_bytes(b" " * (certs_ops.MAX_ACME_BYTES + 1))
    with pytest.raises(OperationError) as caught:
        certs_ops.certs_list({})
    assert caught.value.code == "worker.too_large"


def test_the_hub_schema_has_no_place_for_a_key():
    assert not {"key", "private_key", "certificate"} & set(schemas.Certificate.model_fields)


# --- judging certificates -----------------------------------------------------------------------


def test_wildcards_cover_exactly_one_label_and_expiry_states():
    assert service.covers("*.example.com", "blog.example.com") and not service.covers(
        "*.example.com", "a.b.example.com"
    )
    assert not service.covers("*.example.com", "example.com") and service.covers(
        "Example.COM", "example.com"
    )
    assert [service.state(d) for d in (-1, 0, 20, 21, 90)] == [
        "expired",
        "expiring",
        "expiring",
        "ok",
        "ok",
    ]
    now = datetime(2030, 1, 1, tzinfo=UTC)
    cert = schemas.Certificate(
        resolver="r",
        names=["a.example.com"],
        issuer="x",
        not_before=now - timedelta(days=5),
        not_after=now + timedelta(days=3, hours=2),
        serial="1a",
    )
    assert service.days_left(cert, now) == 3
    expired = cert.model_copy(update={"not_after": now - timedelta(hours=1)})
    assert service.days_left(expired, now) == -1


# --- through the hub ----------------------------------------------------------------------------

SITE = """\
http:
  routers:
    blog: {rule: "Host(`blog.example.com`)", service: s}
    app: {rule: "Host(`app.example.com`)", service: s}
    old: {rule: "Host(`old.example.org`)", service: s}
    none: {rule: "Host(`nocert.example.net`)", service: s}
"""


def start(tmp_path, caplog):
    folder = tmp_path / "dynamic"
    folder.mkdir()
    (folder / "sites.yml").write_text(SITE)
    os.environ["ARMADOK_TRAEFIK_DIR"] = str(folder)
    os.environ["ARMADOK_DOCKER_SOCKET"] = str(tmp_path / "none.sock")
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path / "data", cookie_secure=False, local_node=True),
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
    return client, app, admin, client.get("/api/v1/nodes", headers=admin).json()[0]["id"]


@pytest.fixture(autouse=True)
def clean_env():
    yield
    for name in ("ARMADOK_TRAEFIK_DIR", "ARMADOK_DOCKER_SOCKET"):
        os.environ.pop(name, None)


def test_certificates_and_the_hosts_without_one(tmp_path, caplog, acme):
    client, _, admin, node = start(tmp_path, caplog)
    got = client.get(f"/api/v1/nodes/{node}/certs", headers=admin)
    assert got.status_code == 200 and KEY_MARKER not in got.text
    body = got.json()
    states = {h["host"]: h["state"] for h in body["hosts"]}
    assert states == {
        "blog.example.com": "ok",
        "app.example.com": "expiring",  # covered by the wildcard that ends soon
        "old.example.org": "expired",
        "nocert.example.net": "missing",
    }
    assert {c["state"] for c in body["certificates"]} == {"ok", "expiring", "expired"}
    wildcard = next(c for c in body["certificates"] if "*.example.com" in c["names"])
    assert wildcard["days_left"] in (9, 10) and wildcard["issuer"] == "Test CA / Test CA R1"


def test_an_editor_sees_only_certificates_of_their_own_hosts(tmp_path, caplog, acme):
    client, _, admin, node = start(tmp_path, caplog)
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/nodes/{node}/certs"
    assert client.get(url, headers=bob).status_code == 404  # cannot see the node at all
    for right in ("nodes.read", "certs.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    # He can read certificates but cannot see any site (no traefik.read): so he sees none of them.
    empty = client.get(url, headers=bob).json()
    assert empty["certificates"] == [] and empty["hosts"] == []
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "traefik.read", "scope": f"node:{node}"},
    )
    mine = client.get(url, headers=bob).json()
    assert {c["names"][0] for c in mine["certificates"]} == {
        "blog.example.com",
        "*.example.com",
        "old.example.org",
    }
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "certs.read_all", "scope": f"node:{node}"},
    )
    assert len(client.get(url, headers=bob).json()["certificates"]) == 3


def test_a_certificate_for_someone_elses_domain_stays_hidden_from_an_editor(
    tmp_path, caplog, monkeypatch
):
    folder = tmp_path / "acme"
    folder.mkdir()
    acme_file(
        folder / "acme.json",
        [(["blog.example.com"], 80), (["secret-project.internal.example.net"], 80)],
    )
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(folder / "acme.json"))
    client, _, admin, node = start(tmp_path, caplog)
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    for right in ("nodes.read", "certs.read", "traefik.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    seen = client.get(
        f"/api/v1/nodes/{node}/certs", headers={"Authorization": f"Bearer {token}"}
    ).text
    assert "blog.example.com" in seen and "secret-project" not in seen
    assert "secret-project" in client.get(f"/api/v1/nodes/{node}/certs", headers=admin).text


def test_unconfigured_acme_is_a_clear_error(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("ARMADOK_ACME_FILE", raising=False)
    client, _, admin, node = start(tmp_path, caplog)
    r = client.get(f"/api/v1/nodes/{node}/certs", headers=admin)
    assert r.status_code == 409 and r.json()["error"]["code"] == "worker.acme_not_configured"


def test_expiring_and_expired_certificates_notify_once_a_day(tmp_path, caplog, acme):
    from armadok.modules.notifications.service import wait_for_deliveries

    client, app, admin, node = start(tmp_path, caplog)

    async def go():
        warned = await check_certificates(app)
        await check_certificates(app)  # a second pass inside the cooldown says nothing new
        await wait_for_deliveries(app)
        return warned

    assert asyncio.run(go()) == 2  # one "expired" and one "expiring" message for the node
    titles = [
        e["title"] for e in client.get("/api/v1/notifications", headers=admin).json()["entries"]
    ]
    assert sorted(titles) == ["Certificate expired", "Certificate expiring"]
    mails = json.dumps(client.get("/api/v1/notifications", headers=admin).json())
    assert KEY_MARKER not in mails and "old.example.org" in mails


def test_certificates_appear_on_the_relations_map_linked_to_their_domains(tmp_path, caplog, acme):
    client, _, admin, node = start(tmp_path, caplog)
    graph = client.get(f"/api/v1/nodes/{node}/relations", headers=admin).json()
    certs = [o for o in graph["objects"] if o["type"] == "certificate"]
    assert {c["label"] for c in certs} == {"blog.example.com", "*.example.com", "old.example.org"}
    covers = {
        (k["source"]["type"], k["target"]["id"]) for k in graph["links"] if k["kind"] == "covers"
    }
    assert ("certificate", "blog.example.com") in covers and (
        "certificate",
        "old.example.org",
    ) in covers
    assert KEY_MARKER not in json.dumps(graph)


# --- findings of the security review --------------------------------------------------------------


def test_a_multi_domain_certificate_does_not_reveal_names_the_viewer_cannot_see(
    tmp_path, caplog, monkeypatch
):
    folder = tmp_path / "acme"
    folder.mkdir()
    acme_file(
        folder / "acme.json",
        [(["blog.example.com", "vpn.hidden.example.net", "admin.hidden.example.net"], 80)],
    )
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(folder / "acme.json"))
    client, _, admin, node = start(tmp_path, caplog)
    bob_id = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    for right in ("nodes.read", "certs.read", "traefik.read"):
        client.post(
            f"/api/v1/users/{bob_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    mine = client.get(
        f"/api/v1/nodes/{node}/certs", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert [c["names"] for c in mine["certificates"]] == [["blog.example.com"]]
    assert "hidden" not in json.dumps(mine) and mine["certificates"][0]["serial"] is None
    assert all(h["serial"] is None for h in mine["hosts"])
    full = client.get(f"/api/v1/nodes/{node}/certs", headers=admin).json()
    assert len(full["certificates"][0]["names"]) == 3 and full["certificates"][0]["serial"]


def test_one_odd_certificate_does_not_hide_the_others_and_the_limit_stops_early(
    tmp_path, monkeypatch
):
    file = tmp_path / "acme.json"
    acme_file(file, [(["good.example.com"], 40)])
    doc = json.loads(file.read_text())
    bad = base64.b64encode(
        b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    ).decode()
    doc["letsencrypt"]["Certificates"] += [{"certificate": bad}, {"certificate": 12}, "x", None]
    file.write_text(json.dumps(doc))
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(file))
    assert [c["names"] for c in certs_ops.certs_list({})["certificates"]] == [["good.example.com"]]
    # The limit applies before anything is parsed: the rest of the file costs nothing.
    monkeypatch.setattr(certs_ops, "MAX_CERTIFICATES", 1)
    doc["letsencrypt"]["Certificates"] = doc["letsencrypt"]["Certificates"][:1] * 5
    file.write_text(json.dumps(doc))
    out = certs_ops.certs_list({})
    assert len(out["certificates"]) == 1 and out["truncated"] is True


def test_deeply_nested_json_and_dotted_file_names_give_plain_errors(tmp_path, monkeypatch):
    file = tmp_path / "acme.json"
    file.write_text("[" * 100000 + "]" * 100000)
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(file))
    with pytest.raises(OperationError) as caught:
        certs_ops.certs_list({})
    assert caught.value.code == "worker.acme_invalid"
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(tmp_path / ".hidden"))
    with pytest.raises(OperationError) as caught:
        certs_ops.certs_list({})
    assert caught.value.code == "worker.acme_not_configured"


def test_replaced_certificates_do_not_raise_alarms_and_urgent_names_come_first(
    tmp_path, caplog, monkeypatch
):
    from armadok.modules.certs import watch
    from armadok.modules.notifications.service import wait_for_deliveries

    folder = tmp_path / "acme"
    folder.mkdir()
    # An old expired certificate for blog (replaced by a healthy one), plus four expiring ones.
    acme_file(
        folder / "acme.json",
        [
            (["blog.example.com"], -30),
            (["blog.example.com"], 80),
            (["a.example.net"], 10),
            (["b.example.net"], 9),
            (["c.example.net"], 8),
            (["z.example.net"], 2),
        ],
    )
    monkeypatch.setenv("ARMADOK_ACME_FILE", str(folder / "acme.json"))
    client, app, admin, node = start(tmp_path, caplog)

    async def go():
        await watch.check_certificates(app)
        await wait_for_deliveries(app)

    asyncio.run(go())
    entries = client.get("/api/v1/notifications", headers=admin).json()["entries"]
    assert [e["title"] for e in entries] == [
        "Certificate expiring"
    ]  # the superseded expired one is ignored
    body = entries[0]["body"]
    assert (
        body.index("z.example.net") < body.index("c.example.net") and "(+1)" in body
    )  # most urgent first, count of the rest


def test_the_watch_checks_soon_after_start_and_says_when_it_cannot_check(
    tmp_path, caplog, acme, monkeypatch
):
    from armadok.modules.certs import watch

    client, app, admin, node = start(tmp_path, caplog)
    monkeypatch.setattr(watch, "STARTUP_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(watch, "CHECK_EVERY_SECONDS", 3600)

    async def go():
        task = asyncio.create_task(watch.watch_loop(app))
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert (
        len(client.get("/api/v1/notifications", headers=admin).json()["entries"]) >= 1
    )  # first check was immediate
    monkeypatch.delenv("ARMADOK_ACME_FILE")
    with caplog.at_level(logging.WARNING, logger="armadok.certs"):
        asyncio.run(watch.check_certificates(app))
    assert any(
        "skipped" in r.getMessage() and "worker.acme_not_configured" in r.getMessage()
        for r in caplog.records
    )
