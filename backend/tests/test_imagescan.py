import io
import json
import logging
import re
import tarfile

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.imagescan import service
from armadok.modules.vulns import osv
from armadok.worker import image_ops
from armadok.worker.operation import OperationError

PASSWORD = "correct horse battery"
IMAGE = "sha256:" + "a" * 64

DPKG = """Package: libssl3
Status: install ok installed
Version: 3.0.11-1~deb12u2
Source: openssl (3.0.11-1~deb12u2)
Description: Secure Sockets Layer toolkit
 spread over
 several lines

Package: bash
Status: install ok installed
Version: 5.2.15-2+b2
Source: bash (5.2.15-2)

Package: removed-thing
Status: deinstall ok config-files
Version: 1.0

Package: evil/name
Status: install ok installed
Version: 1.0
"""
APK = """P:musl
V:1.2.4-r2
o:musl

P:libcrypto3
V:3.1.4-r0
o:openssl
"""
OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 12"\nID=debian\nVERSION_ID="12"\n'


def archive_of(content: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        data = content.encode()
        info = tarfile.TarInfo("file")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_the_package_lists_are_read_by_source_package_and_junk_is_dropped():
    assert image_ops.parse_dpkg(DPKG) == [("openssl", "3.0.11-1~deb12u2"), ("bash", "5.2.15-2")]
    assert image_ops.parse_apk(APK) == [("musl", "1.2.4-r2"), ("openssl", "3.1.4-r0")]
    assert image_ops.parse_os(OS_RELEASE) == ("debian", "12")
    assert image_ops.parse_os("ID=$(reboot)\nVERSION_ID=../x") == ("", "")


def fake_docker(monkeypatch, files, calls):
    def raw(method, url, timeout=10, body=None):
        calls.append((method, url.split("?")[0]))
        if method == "POST" and url.startswith("/containers/create"):
            calls.append(("body", json.dumps(body)))
            return 201, json.dumps({"Id": "b" * 64}).encode()
        if method == "GET" and "/archive" in url:
            path = re.search(r"path=([^&]+)", url).group(1).replace("%2F", "/")
            return (200, archive_of(files[path])) if path in files else (404, b"")
        return 204, b""

    monkeypatch.setattr(image_ops, "docker_raw", raw)


def test_an_image_is_read_without_ever_being_started_and_the_container_is_removed(monkeypatch):
    calls: list = []
    fake_docker(monkeypatch, {"/etc/os-release": OS_RELEASE, "/var/lib/dpkg/status": DPKG}, calls)
    answer = image_ops.image_packages({"image": IMAGE})
    assert answer["os"] == "debian" and answer["os_version"] == "12"
    assert {p["name"] for p in answer["packages"]} == {"openssl", "bash"}
    assert not any("/start" in url for _, url in calls)  # none of the image's code runs
    assert calls[-1][0] == "DELETE"
    created = json.loads(next(v for k, v in calls if k == "body"))
    assert created["NetworkDisabled"] is True and created["HostConfig"]["NetworkMode"] == "none"
    assert created["Labels"]["armadok.protect"] == "true"


def test_bad_image_ids_never_reach_the_daemon_and_an_unknown_image_is_not_found(monkeypatch):
    calls: list = []
    fake_docker(monkeypatch, {}, calls)
    for bad in (None, "", "alpine:latest", "sha256:abc", IMAGE + "x", "../../etc"):
        with pytest.raises(OperationError) as error:
            image_ops.image_packages({"image": bad})
        assert error.value.code == "worker.invalid_path"
    assert calls == []
    monkeypatch.setattr(image_ops, "docker_raw", lambda *a, **k: (404, b""))
    with pytest.raises(OperationError) as error:
        image_ops.image_packages({"image": IMAGE})
    assert error.value.code == "worker.not_found"


def test_an_image_without_a_package_list_answers_empty_and_is_still_cleaned_up(monkeypatch):
    calls: list = []
    fake_docker(monkeypatch, {}, calls)
    assert image_ops.image_packages({"image": IMAGE})["packages"] == []
    assert calls[-1][0] == "DELETE"


def test_osv_knows_only_the_systems_it_covers():
    assert service.ecosystem("debian", "12") == "Debian:12"
    assert service.ecosystem("ubuntu", "22.04") == "Ubuntu:22.04"
    assert service.ecosystem("alpine", "3.19.1") == "Alpine:v3.19"
    assert service.ecosystem("debian", "sid") is None and service.ecosystem("arch", "") is None


@pytest.fixture
def box(tmp_path, caplog):
    service._busy.clear()
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
                vuln_check=True,
            )
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).json()["access_token"]
        admin = {"Authorization": f"Bearer {token}"}
        node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
        yield client, admin, node, app


def wire(monkeypatch, severity="high"):
    sent = []

    async def fake_call(app, registry, node_id, op, args, actor_id=None):
        if op == "docker.containers.list":
            return {"containers": [{"id": "c" * 12, "names": ["web"], "image": "web:1"}]}
        if op == "docker.images.list":
            return {"items": [{"id": IMAGE, "tags": ["web:1"], "size": 1, "created": 1}]}
        return {
            "os": "debian",
            "os_version": "12",
            "packages": [{"name": "openssl", "version": "3.0.11-1"}],
        }

    async def fake_scan(components, ecosystem="PyPI", **limits):
        sent.append((components, ecosystem))
        finding = {
            "component": "openssl", "version": "3.0.11-1", "id": "DSA-1", "severity": severity,
            "summary": "bad", "fixed": "3.0.12", "aliases": [],
        }  # fmt: skip
        return [finding], set()

    async def fake_emit(app, name, /, **payload):
        sent.append((name, payload))

    monkeypatch.setattr(service, "call_operation", fake_call)
    monkeypatch.setattr(osv, "scan", fake_scan)
    monkeypatch.setattr(service, "emit", fake_emit)
    return sent


def test_the_images_in_use_are_scanned_and_new_serious_flaws_are_announced_once(box, monkeypatch):
    client, admin, node, _ = box
    sent = wire(monkeypatch)
    from armadok.modules.imagescan import routes

    monkeypatch.setattr(routes, "MIN_GAP_SECONDS", 0)
    done = client.post(f"/api/v1/nodes/{node}/imagescan/scan", headers=admin, json={})
    assert done.status_code == 200 and done.json()["results"][0]["status"] == "ok"
    assert sent[0] == ([("openssl", "3.0.11-1")], "Debian:12")  # only names and versions leave
    assert [s for s in sent if s[0] == "notify"][0][1]["params"]["count"] == 1
    shown = client.get(f"/api/v1/nodes/{node}/imagescan", headers=admin).json()
    assert shown["scans"][0]["counts"] == {"high": 1} and shown["can_run"] is True
    assert shown["scans"][0]["name"] == "web:1"
    client.post(f"/api/v1/nodes/{node}/imagescan/scan", headers=admin, json={"image": IMAGE})
    assert len([s for s in sent if s[0] == "notify"]) == 1  # not announced again


def test_scanning_is_off_without_the_switch_limited_and_needs_its_right(box, monkeypatch):
    client, admin, node, app = box
    wire(monkeypatch)
    from armadok.modules.imagescan import routes

    url = f"/api/v1/nodes/{node}/imagescan/scan"
    assert client.post(url, headers=admin, json={"image": "alpine"}).status_code == 422
    assert client.post(url, headers=admin, json={"image": "sha256:" + "f" * 64}).status_code == 404
    routes._last.clear()
    assert client.post(url, headers=admin, json={}).status_code == 200
    assert client.post(url, headers=admin, json={}).status_code == 429  # too soon
    object.__setattr__(app.state.settings, "vuln_check", False)
    routes._last.clear()
    assert client.post(url, headers=admin, json={}).status_code == 409
    other = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()
    assert other["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bo = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/v1/nodes/{node}/imagescan", headers=bo).status_code == 404


def test_leftover_scan_containers_are_swept_but_a_fresh_one_is_left_alone(monkeypatch):
    import time as clock

    calls: list = []
    old, fresh = "d" * 64, "e" * 64

    def raw(method, url, timeout=10, body=None):
        calls.append((method, url.split("?")[0]))
        if method == "GET" and url.startswith("/containers/json"):
            listing = [
                {"Id": old, "Created": int(clock.time()) - 3600},
                {"Id": fresh, "Created": int(clock.time())},
                {"Id": "nonsense", "Created": 0},
            ]
            return 200, json.dumps(listing).encode()
        if method == "POST":
            return 201, json.dumps({"Id": "b" * 64}).encode()
        return (404, b"") if "archive" in url else (204, b"")

    monkeypatch.setattr(image_ops, "docker_raw", raw)
    image_ops.image_packages({"image": IMAGE})
    deleted = [u for m, u in calls if m == "DELETE"]
    assert f"/containers/{old}" in deleted and f"/containers/{fresh}" not in deleted
    assert "/containers/nonsense" not in deleted
    assert f"/containers/{'b' * 64}" in deleted  # its own container is always removed


def test_a_failing_cleanup_does_not_hide_the_result(monkeypatch):
    def raw(method, url, timeout=10, body=None):
        if method == "DELETE":
            raise OperationError("worker.docker_unavailable")
        if method == "POST":
            return 201, json.dumps({"Id": "b" * 64}).encode()
        return (200, archive_of(OS_RELEASE)) if "os-release" in url else (404, b"")

    monkeypatch.setattr(image_ops, "docker_raw", raw)
    assert image_ops.image_packages({"image": IMAGE})["os"] == "debian"


def test_results_of_images_the_machine_no_longer_has_are_forgotten_and_truncation_shows(
    box, monkeypatch
):
    client, admin, node, app = box
    wire(monkeypatch)
    from armadok.modules.imagescan import repository, routes

    monkeypatch.setattr(routes, "MIN_GAP_SECONDS", 0)
    conn = app.state.settings.open_db()
    repository.store(
        conn, node, "sha256:" + "9" * 64, "gone:1", os_id="debian", os_version="12",
        packages=1, status="ok", findings=[],
    )  # fmt: skip
    conn.close()
    client.post(f"/api/v1/nodes/{node}/imagescan/scan", headers=admin, json={})
    names = [
        s["name"]
        for s in client.get(f"/api/v1/nodes/{node}/imagescan", headers=admin).json()["scans"]
    ]
    assert names == ["web:1"]
    # A package list cut short is said so, instead of looking clean.
    orig = service.call_operation

    async def cut(app_, registry, node_id, op, args, actor_id=None):
        answer = await orig(app_, registry, node_id, op, args, actor_id)
        return {**answer, "truncated": True} if op == "images.packages" else answer

    monkeypatch.setattr(service, "call_operation", cut)
    client.post(f"/api/v1/nodes/{node}/imagescan/scan", headers=admin, json={})
    shown = client.get(f"/api/v1/nodes/{node}/imagescan", headers=admin).json()["scans"][0]
    assert shown["status"] == "partial"
