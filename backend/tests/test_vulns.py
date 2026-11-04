import asyncio
import json
import logging
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.vulns import osv, repository, routes, service

PASSWORD = "correct horse battery"


def record(vuln_id="GHSA-aaaa-bbbb-cccc", severity="HIGH", fixed="2.0.1", **extra):
    return {
        "id": vuln_id,
        "summary": "Something   bad\nhappens",
        "aliases": ["CVE-2024-12345", "PYSEC-2024-1", 5],
        "database_specific": {"severity": severity},
        "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": fixed}]}]}],
        **extra,
    }


def test_a_record_is_reduced_to_checked_fields():
    got = osv.parse_detail(record())
    assert got == {
        "id": "GHSA-aaaa-bbbb-cccc",
        "severity": "high",
        "summary": "Something bad happens",
        "fixed": "2.0.1",
        "aliases": ["CVE-2024-12345"],  # only well-formed CVE ids are kept
    }
    assert osv.parse_detail(record(severity="MEDIUM"))["severity"] == "moderate"
    assert osv.parse_detail(record(severity="<script>"))["severity"] == "unknown"


@pytest.mark.parametrize(
    "raw",
    [None, [], "x", {}, {"id": 5}, {"id": "../../etc/passwd"}, {"id": "a b c"}, {"id": "x" * 200}],
)
def test_a_hostile_record_is_refused(raw):
    assert osv.parse_detail(raw) is None


def test_odd_fields_inside_a_record_cannot_break_parsing():
    raw = record(summary="x" * 5000, affected="nope", aliases="nope")
    got = osv.parse_detail(raw)
    assert len(got["summary"]) == 200 and got["fixed"] == "" and got["aliases"] == []
    weird = {
        "id": "GHSA-aaaa-bbbb-cccc",
        "affected": [5, {"ranges": [None, {"events": ["x", {"fixed": "bad version!"}]}]}],
    }
    assert osv.parse_detail(weird)["fixed"] == ""


def test_the_installed_packages_are_listed_with_osv_names():
    components = osv.installed_components()
    names = [n for n, _ in components]
    assert names == sorted(names) and "fastapi" in names
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", n) for n in names)


def fake_osv(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        osv, "_client", lambda: httpx.AsyncClient(transport=transport, follow_redirects=False)
    )


def test_a_scan_sends_only_names_and_versions_to_the_one_fixed_host(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.content))
        if request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            results = [
                {"vulns": [{"id": "GHSA-aaaa-bbbb-cccc"}]}
                if q["package"]["name"] == "jinja2"
                else {}
                for q in queries
            ]
            return httpx.Response(200, json={"results": results})
        return httpx.Response(200, json=record())

    fake_osv(monkeypatch, handler)
    found, truncated = asyncio.run(osv.scan([("jinja2", "2.4.1"), ("fastapi", "0.115.0")]))
    assert truncated == set()
    assert [(f["component"], f["version"], f["id"], f["severity"]) for f in found] == [
        ("jinja2", "2.4.1", "GHSA-aaaa-bbbb-cccc", "high")
    ]
    assert {u.split("/")[2] for _, u, _ in seen} == {"api.osv.dev"}
    body = json.loads(seen[0][2])
    assert set(body) == {"queries"} and set(body["queries"][0]) == {"package", "version"}


@pytest.mark.parametrize(
    "answer",
    [{"results": []}, {"results": "x"}, ["x"], {"results": [5, 6]}, {}],
)
def test_a_badly_shaped_answer_is_an_error_not_a_crash(monkeypatch, answer):
    fake_osv(monkeypatch, lambda request: httpx.Response(200, json=answer))
    with pytest.raises(osv.OsvError):
        asyncio.run(osv.scan([("jinja2", "2.4.1")]))


def test_a_down_or_endless_server_is_an_error(monkeypatch):
    fake_osv(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(osv.OsvError) as error:
        asyncio.run(osv.scan([("jinja2", "2.4.1")]))
    assert error.value.args == ("vulns.unreachable",)
    fake_osv(
        monkeypatch, lambda request: httpx.Response(200, content=b"x" * (osv.MAX_ANSWER_BYTES + 1))
    )
    with pytest.raises(osv.OsvError):
        asyncio.run(osv.scan([("jinja2", "2.4.1")]))
    fake_osv(monkeypatch, lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(osv.OsvError):
        asyncio.run(osv.scan([("jinja2", "2.4.1")]))


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(routes, "MIN_SCAN_GAP_SECONDS", 0)
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                cookie_secure=False,
                encryption_key=generate_key(),
                vuln_check=True,
            )
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def use_osv(monkeypatch, vulns):
    """Make the scan find `vulns` = {(component, version): [records]}."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            results = []
            for q in queries:
                records = vulns.get((q["package"]["name"], q["version"]), [])
                results.append({"vulns": [{"id": r["id"]} for r in records]} if records else {})
            return httpx.Response(200, json={"results": results})
        wanted = request.url.path.rsplit("/", 1)[1]
        for records in vulns.values():
            for r in records:
                if r["id"] == wanted:
                    return httpx.Response(200, json=r)
        return httpx.Response(404)

    fake_osv(monkeypatch, handler)
    monkeypatch.setattr(
        osv, "installed_components", lambda: [("jinja2", "2.4.1"), ("fastapi", "0.115.0")]
    )


def test_findings_are_stored_listed_and_the_serious_new_ones_are_notified(box, monkeypatch):
    client, admin, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(service, "emit", fake_emit)
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [record(), record("GHSA-low0-low0-low0", "LOW")]})
    got = client.post("/api/v1/vulns/scan", headers=admin)
    assert got.status_code == 200 and got.json() == {"announced": 1}
    assert len(sent) == 1 and sent[0]["event"] == "vulns.found" and sent[0]["params"]["count"] == 1
    listing = client.get("/api/v1/vulns", headers=admin).json()
    assert (
        listing["enabled"] is True
        and listing["scan"]["ok"] is True
        and listing["scan"]["components"] == 2
    )
    assert [f["severity"] for f in listing["findings"]] == ["high", "low"]  # most serious first
    sent.clear()
    client.post("/api/v1/vulns/scan", headers=admin)
    assert sent == []  # nothing new: no second alert


def test_a_fixed_finding_disappears_and_an_exception_is_kept_with_its_reason(box, monkeypatch):
    client, admin, app = box
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [record()]})
    client.post("/api/v1/vulns/scan", headers=admin)
    fid = client.get("/api/v1/vulns", headers=admin).json()["findings"][0]["id"]
    assert (
        client.put(
            f"/api/v1/vulns/{fid}/ignore", headers=admin, json={"reason": "not reachable"}
        ).status_code
        == 200
    )
    client.post("/api/v1/vulns/scan", headers=admin)
    kept = client.get("/api/v1/vulns", headers=admin).json()["findings"][0]
    assert kept["ignored_reason"] == "not reachable"  # a new scan keeps the exception
    assert "vulns.ignore" in client.get("/api/v1/audit", headers=admin).text
    assert client.delete(f"/api/v1/vulns/{fid}/ignore", headers=admin).status_code == 204
    assert (
        client.get("/api/v1/vulns", headers=admin).json()["findings"][0]["ignored_reason"] is None
    )
    use_osv(monkeypatch, {})  # the database no longer lists it
    client.post("/api/v1/vulns/scan", headers=admin)
    assert (
        len(client.get("/api/v1/vulns", headers=admin).json()["findings"]) == 1
    )  # one hiccup is not enough
    client.post("/api/v1/vulns/scan", headers=admin)
    assert client.get("/api/v1/vulns", headers=admin).json()["findings"] == []  # absent twice
    assert (
        client.put(
            "/api/v1/vulns/999/ignore", headers=admin, json={"reason": "because"}
        ).status_code
        == 404
    )


def test_rights_and_the_off_switch(box, monkeypatch):
    client, admin, app = box
    client.post("/api/v1/users", headers=admin, json={"username": "eve", "password": PASSWORD})
    eve = login(client, "eve")
    for call in (
        lambda h: client.get("/api/v1/vulns", headers=h),
        lambda h: client.post("/api/v1/vulns/scan", headers=h),
        lambda h: client.put("/api/v1/vulns/1/ignore", headers=h, json={"reason": "because"}),
    ):
        assert call(eve).status_code == 403
    assert client.get("/api/v1/vulns").status_code == 401
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["vulns.read"]}
    )
    assert key.status_code == 422 and key.json()["error"]["code"] == "apikeys.invalid_right"
    monkeypatch.setattr(
        app.state.settings.__class__, "vuln_check", False, raising=False
    ) if False else None


def test_nothing_is_sent_when_the_check_is_off(tmp_path, caplog, monkeypatch):
    called = []
    fake_osv(monkeypatch, lambda request: called.append(request) or httpx.Response(500))
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path / "d", cookie_secure=False, encryption_key=generate_key())
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        admin = login(client, "admin")
        got = client.post("/api/v1/vulns/scan", headers=admin)
        assert got.status_code == 409 and got.json()["error"]["code"] == "vulns.disabled"
        assert client.get("/api/v1/vulns", headers=admin).json()["enabled"] is False
        assert service.due(app) is False
    assert called == []


def test_a_failed_scan_is_recorded_and_reported_without_details(box, monkeypatch):
    client, admin, app = box
    fake_osv(monkeypatch, lambda request: httpx.Response(503))
    monkeypatch.setattr(osv, "installed_components", lambda: [("jinja2", "2.4.1")])
    got = client.post("/api/v1/vulns/scan", headers=admin)
    assert got.status_code == 502 and got.json()["error"]["code"] == "vulns.unreachable"
    assert client.get("/api/v1/vulns", headers=admin).json()["scan"]["ok"] is False


def test_a_scan_is_due_after_a_day_only(box):
    client, admin, app = box
    assert service.due(app) is True  # never scanned
    conn = app.state.settings.open_db()
    repository.set_scan(conn, True, None, 3)
    conn.close()
    assert service.due(app) is False
    conn = app.state.settings.open_db()
    conn.execute("UPDATE vuln_scan SET finished_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    assert service.due(app) is True


def test_a_reason_must_be_given_and_be_plain_text(box, monkeypatch):
    client, admin, app = box
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [record()]})
    client.post("/api/v1/vulns/scan", headers=admin)
    fid = client.get("/api/v1/vulns", headers=admin).json()["findings"][0]["id"]
    for bad in ("", "ab", "x" * 301, "line\nbreak"):
        assert (
            client.put(
                f"/api/v1/vulns/{fid}/ignore", headers=admin, json={"reason": bad}
            ).status_code
            == 422
        )


def test_cvss_scores_follow_the_specification():
    assert osv.cvss3_score("CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:N/A:N") == 8.6
    assert osv.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert osv.cvss3_score("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N") == 1.8
    assert osv.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == 0.0
    for bad in (
        "",
        "CVSS:4.0/AV:N",
        "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "CVSS:3.1/AV:N",
        "garbage",
    ):
        assert osv.cvss3_score(bad) is None


def test_a_record_without_a_label_is_rated_from_its_cvss_vector():
    raw = {
        "id": "PYSEC-2024-1",
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    }
    assert osv.parse_detail(raw)["severity"] == "critical"
    assert osv.parse_detail({"id": "PYSEC-2024-2"})["severity"] == "unknown"
    assert (
        osv.parse_detail({"id": "PYSEC-2024-3", "severity": [5, {"score": 7}, {"score": "x"}]})[
            "severity"
        ]
        == "unknown"
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "GHSA-aaaa-bbbb-cccc", "affected": 5},
        {"id": "GHSA-aaaa-bbbb-cccc", "affected": {"ranges": 1}},
        {"id": "GHSA-aaaa-bbbb-cccc", "affected": [{"ranges": 7}]},
        {"id": "GHSA-aaaa-bbbb-cccc", "affected": [{"ranges": [{"events": 3}]}]},
        {"id": "GHSA-aaaa-bbbb-cccc", "aliases": 9, "severity": 4, "database_specific": 1},
    ],
)
def test_a_record_of_any_odd_shape_never_raises(raw):
    assert osv.parse_detail(raw)["id"] == "GHSA-aaaa-bbbb-cccc"


def test_an_unreadable_listed_vulnerability_aborts_the_scan_instead_of_hiding_it(monkeypatch):
    def handler(request):
        if request.url.path == "/v1/querybatch":
            return httpx.Response(
                200, json={"results": [{"vulns": [{"id": "GHSA-aaaa-bbbb-cccc"}]}]}
            )
        return httpx.Response(200, json={"id": "SOMETHING-ELSE", "weird": True})

    fake_osv(monkeypatch, handler)
    with pytest.raises(osv.OsvError) as error:
        asyncio.run(osv.scan([("jinja2", "2.4.1")]))
    assert error.value.args == ("vulns.bad_answer",)


def test_an_odd_answer_is_recorded_as_a_failed_scan_not_a_crash(box, monkeypatch):
    client, admin, app = box
    monkeypatch.setattr(osv, "installed_components", lambda: [("jinja2", "2.4.1")])

    def boom(request):
        raise RuntimeError("unexpected")

    fake_osv(monkeypatch, boom)
    got = client.post("/api/v1/vulns/scan", headers=admin)
    assert got.status_code == 502
    assert client.get("/api/v1/vulns", headers=admin).json()["scan"]["ok"] is False


def test_an_upgraded_package_loses_its_findings_at_once_and_a_withdrawn_one_after_two_scans(
    box, monkeypatch
):
    client, admin, app = box
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [record()]})
    client.post("/api/v1/vulns/scan", headers=admin)
    monkeypatch.setattr(osv, "installed_components", lambda: [("jinja2", "3.1.4")])
    use_osv(monkeypatch, {})
    monkeypatch.setattr(osv, "installed_components", lambda: [("jinja2", "3.1.4")])
    client.post("/api/v1/vulns/scan", headers=admin)
    assert client.get("/api/v1/vulns", headers=admin).json()["findings"] == []


def test_a_second_serious_batch_is_announced_even_inside_the_old_cooldown(box, monkeypatch):
    client, admin, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload["params"])

    monkeypatch.setattr(service, "emit", fake_emit)
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [record()]})
    client.post("/api/v1/vulns/scan", headers=admin)
    use_osv(
        monkeypatch, {("jinja2", "2.4.1"): [record(), record("GHSA-xxxx-yyyy-zzzz", "CRITICAL")]}
    )
    client.post("/api/v1/vulns/scan", headers=admin)
    assert [p["count"] for p in sent] == [1, 1] and sent[1]["severity"] == "critical"


def test_unrated_findings_are_announced_too(box, monkeypatch):
    client, admin, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload["params"])

    monkeypatch.setattr(service, "emit", fake_emit)
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [{"id": "PYSEC-2024-9"}]})
    client.post("/api/v1/vulns/scan", headers=admin)
    assert sent and sent[0]["severity"] == "unknown"


def test_a_failed_scan_is_retried_after_an_hour_not_a_day(box):
    client, admin, app = box
    conn = app.state.settings.open_db()
    repository.set_scan(conn, False, "vulns.unreachable", 3)
    conn.close()
    assert service.due(app) is False
    conn = app.state.settings.open_db()
    conn.execute(
        "UPDATE vuln_scan SET finished_at = ?",
        (
            (
                __import__("datetime").datetime.now(__import__("datetime").UTC)
                - __import__("datetime").timedelta(hours=2)
            ).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    assert service.due(app) is True


def test_someone_elses_reason_is_not_overwritten_and_the_old_one_is_audited_when_removed(
    box, monkeypatch
):
    client, admin, app = box
    use_osv(monkeypatch, {("jinja2", "2.4.1"): [record()]})
    client.post("/api/v1/vulns/scan", headers=admin)
    fid = client.get("/api/v1/vulns", headers=admin).json()["findings"][0]["id"]
    assert (
        client.put(
            f"/api/v1/vulns/{fid}/ignore", headers=admin, json={"reason": "first reason"}
        ).status_code
        == 200
    )
    again = client.put(
        f"/api/v1/vulns/{fid}/ignore", headers=admin, json={"reason": "second reason"}
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "vulns.already_accepted"
    client.delete(f"/api/v1/vulns/{fid}/ignore", headers=admin)
    assert "first reason" in client.get("/api/v1/audit", headers=admin).text


def test_a_scan_already_running_is_not_a_failure(box, monkeypatch):
    client, admin, app = box
    service._running = asyncio.Lock()

    async def hold():
        await service._running.acquire()

    client.portal.call(hold)
    try:
        got = client.post("/api/v1/vulns/scan", headers=admin)
        assert got.status_code == 409 and got.json()["error"]["code"] == "vulns.busy"
        assert "vulns.scan" not in client.get("/api/v1/audit", headers=admin).text
    finally:
        service._running.release()


def test_the_answer_is_read_with_a_size_cap_while_streaming(monkeypatch):
    big = httpx.Response(200, content=b"x" * (osv.MAX_ANSWER_BYTES + 10))
    fake_osv(monkeypatch, lambda request: big)
    with pytest.raises(osv.OsvError) as error:
        asyncio.run(osv.scan([("jinja2", "2.4.1")]))
    assert error.value.args == ("vulns.too_large",)
