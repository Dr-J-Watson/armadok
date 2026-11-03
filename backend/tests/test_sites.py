import json
import logging
import os
import re
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.traefik import sitebuilder
from armadok.modules.traefik.sitebuilder import SiteSpec
from armadok.worker import history, safefs, sites_ops, traefik_ops
from armadok.worker.operation import OperationError

PASSWORD = "correct horse battery"
MODULES = ("users", "apikeys", "nodes", "workers", "traefik", "audit", "relations")


def spec(**over):
    base = {"name": "blog", "hosts": ["blog.example.com"], "container": "blog-web", "port": 80}
    return SiteSpec(**{**base, **over})


# --- the generated text ---------------------------------------------------------------------


def test_a_site_becomes_a_router_and_a_service():
    text = sitebuilder.render(
        spec(
            hosts=["a.example.com", "B.example.com"],
            path_prefix="/app",
            middlewares=["auth@file"],
            cert_resolver="le",
        )
    )
    document = yaml.safe_load(text)
    router = document["http"]["routers"]["blog"]
    assert (
        router["rule"] == "(Host(`a.example.com`) || Host(`b.example.com`)) && PathPrefix(`/app`)"
    )
    assert router["entryPoints"] == ["websecure"] and router["tls"] == {"certResolver": "le"}
    assert router["middlewares"] == ["auth@file"] and router["service"] == "blog"
    assert document["http"]["services"]["blog"]["loadBalancer"]["servers"] == [
        {"url": "http://blog-web:80"}
    ]


def test_without_https_the_site_uses_the_plain_entrypoint_and_no_tls():
    router = yaml.safe_load(sitebuilder.render(spec(https=False)))["http"]["routers"]["blog"]
    assert router["entryPoints"] == ["web"] and "tls" not in router


@pytest.mark.parametrize(
    "bad",
    [
        {"hosts": ["a.example.com`) || Host(`evil.com"]},
        {"hosts": ["bad host.com"]},
        {"hosts": ["nodot"]},
        {"hosts": []},
        {"hosts": ["a.example.com", "A.example.com"]},
        {"path_prefix": "/x`)"},
        {"path_prefix": "x"},
        {"name": "Has Space"},
        {"name": "../x"},
        {"name": "-x"},
        {"name": "a" * 41},
        {"container": "web\nevil: 1"},
        {"container": None, "port": None},
        {"port": 0},
        {"port": 70000},
        {"container": None, "url": "http://a:80", "port": None, "x": 1},
        {"middlewares": ["a b"]},
        {"middlewares": ["x" * 70]},
        {"entrypoints": ["web\n"]},
        {"cert_resolver": "le", "https": False},
        {"cert_resolver": "le\nx"},
    ],
)
def test_nothing_unsafe_gets_into_a_file(bad):
    with pytest.raises((ValidationError, ValueError, TypeError)):
        spec(**bad)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@host:80",
        "http://host/path",
        "http://host?q=1",
        "ftp://host",
        "http://",
        "http://ho st",
        "file:///etc",
    ],
)
def test_upstream_addresses_are_plain(url):
    with pytest.raises(ValidationError):
        spec(container=None, port=None, url=url)


def test_a_plain_address_target_is_kept_without_extras():
    assert (
        spec(container=None, port=None, url="https://10.0.0.5:8443/").server_address()
        == "https://10.0.0.5:8443"
    )


def test_the_file_can_be_read_back_but_only_if_nobody_edited_it():
    original = spec()
    text = sitebuilder.render(original)
    assert sitebuilder.spec_of(text) == original and sitebuilder.untouched(text) == original
    assert sitebuilder.untouched(text + "# my own note\n") is None
    assert sitebuilder.untouched(text.replace("url: http://blog-web", "url: http://other")) is None
    assert sitebuilder.spec_of("http: {}\n") is None


# --- the worker, on a real folder -----------------------------------------------------------


@pytest.fixture
def folder(tmp_path, monkeypatch):
    root = tmp_path / "dyn"
    root.mkdir()
    monkeypatch.setenv("ARMADOK_TRAEFIK_DIR", str(root))
    return root


def sha(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def test_pause_renames_and_resume_restores_the_exact_file(folder):
    text = sitebuilder.render(spec())
    sites_ops.site_write({"name": "blog", "content": text, "expected_sha256": None})
    assert (folder / "sites" / "blog.yml").read_text() == text
    sites_ops.site_pause({"name": "blog"})
    assert not (folder / "sites" / "blog.yml").exists()
    paused = sites_ops.sites_list({})["files"]
    assert paused[0]["paused"] is True and paused[0]["content"] == text
    # Traefik reads only yml/yaml/toml files: the paused file must not look like one.
    assert not (folder / "sites" / "blog.yml.paused").name.endswith((".yml", ".yaml", ".toml"))
    sites_ops.site_resume({"name": "blog"})
    assert (folder / "sites" / "blog.yml").read_text() == text


def test_creating_never_replaces_and_a_paused_site_keeps_its_name(folder):
    sites_ops.site_write({"name": "blog", "content": "a: 1\n", "expected_sha256": None})
    with pytest.raises(OperationError) as exists:
        sites_ops.site_write({"name": "blog", "content": "b: 2\n", "expected_sha256": None})
    assert exists.value.code == "worker.conflict"
    sites_ops.site_pause({"name": "blog"})
    with pytest.raises(OperationError) as paused:
        sites_ops.site_write({"name": "blog", "content": "c: 3\n", "expected_sha256": None})
    assert paused.value.code == "worker.conflict"
    assert (folder / "sites" / "blog.yml.paused").read_text() == "a: 1\n"


def test_resume_does_not_overwrite_a_file_that_appeared_meanwhile(folder):
    sites_ops.site_write({"name": "blog", "content": "a: 1\n", "expected_sha256": None})
    sites_ops.site_pause({"name": "blog"})
    (folder / "sites" / "blog.yml").write_text("other: 1\n")
    with pytest.raises(OperationError) as caught:
        sites_ops.site_resume({"name": "blog"})
    assert caught.value.code == "worker.conflict"
    assert (folder / "sites" / "blog.yml.paused").read_text() == "a: 1\n"


@pytest.mark.parametrize("name", ["../x", "A", "a b", "", None, 5, "x" * 41, "a/b", ".hidden"])
def test_site_names_are_checked_by_the_worker_too(folder, name):
    for op in (sites_ops.site_pause, sites_ops.site_resume):
        with pytest.raises(OperationError):
            op({"name": name})
    with pytest.raises(OperationError):
        sites_ops.site_write({"name": name, "content": "a: 1\n", "expected_sha256": None})


def test_links_are_never_followed(folder, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    (folder / "sites").mkdir()
    (folder / "sites" / "blog.yml").symlink_to(secret)
    assert sites_ops.sites_list({})["files"] == []
    with pytest.raises(OperationError):
        sites_ops.site_pause({"name": "blog"})
    assert secret.read_text() == "top secret"
    (folder / "sites").rmdir() if False else None


def test_a_linked_sites_folder_is_refused(folder, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / "sites").symlink_to(outside)
    with pytest.raises(OperationError):
        sites_ops.site_write({"name": "blog", "content": "a: 1\n", "expected_sha256": None})
    assert list(outside.iterdir()) == []


def test_delete_needs_the_version_the_caller_saw_and_keeps_a_copy(folder):
    sites_ops.site_write({"name": "blog", "content": "a: 1\n", "expected_sha256": None})
    with pytest.raises(OperationError) as stale:
        sites_ops.site_delete({"name": "blog", "expected_sha256": "0" * 64})
    assert stale.value.code == "worker.conflict" and (folder / "sites" / "blog.yml").exists()
    sites_ops.site_delete({"name": "blog", "expected_sha256": sha("a: 1\n")})
    assert not (folder / "sites" / "blog.yml").exists()
    versions = sites_ops.history_list({"path": "sites/blog.yml"})["versions"]
    assert len(versions) == 1
    assert (
        sites_ops.history_read({"path": "sites/blog.yml", "version": versions[0]["version"]})[
            "content"
        ]
        == "a: 1\n"
    )


def test_every_replacement_keeps_the_previous_text_up_to_twenty_versions(folder):
    traefik_ops.file_write({"path": "app.yml", "content": "v: 0\n", "expected_sha256": None})
    current = "v: 0\n"
    for i in range(1, 25):
        new = f"v: {i}\n"
        traefik_ops.file_write({"path": "app.yml", "content": new, "expected_sha256": sha(current)})
        current = new
    versions = sites_ops.history_list({"path": "app.yml"})["versions"]
    assert len(versions) == history.KEEP_VERSIONS
    newest = sites_ops.history_read({"path": "app.yml", "version": versions[0]["version"]})
    assert newest["content"] == "v: 23\n"  # the text just before the last change
    # Saving the same text again is not a new version.
    traefik_ops.file_write({"path": "app.yml", "content": current, "expected_sha256": sha(current)})
    assert len(sites_ops.history_list({"path": "app.yml"})["versions"]) == history.KEEP_VERSIONS


def test_history_is_kept_apart_from_the_configuration(folder):
    traefik_ops.file_write({"path": "app.yml", "content": "v: 0\n", "expected_sha256": None})
    traefik_ops.file_write(
        {"path": "app.yml", "content": "v: 1\n", "expected_sha256": sha("v: 0\n")}
    )
    assert [f["path"] for f in traefik_ops.files_list({})["files"]] == ["app.yml"]
    kept = list((folder / ".armadok-history").iterdir())
    assert kept and all(p.name.endswith(".bak") for p in kept)


@pytest.mark.parametrize(
    "version", ["../../etc/passwd", "1" * 13, "x", None, 5, "0" * 13 + "." + "g" * 8]
)
def test_history_versions_are_validated(folder, version):
    with pytest.raises(OperationError):
        sites_ops.history_read({"path": "app.yml", "version": version})


def test_history_folder_link_is_refused(folder, tmp_path):
    outside = tmp_path / "out"
    outside.mkdir()
    (folder / ".armadok-history").symlink_to(outside)
    traefik_ops.file_write({"path": "a.yml", "content": "x: 1\n", "expected_sha256": None})
    traefik_ops.file_write({"path": "a.yml", "content": "x: 2\n", "expected_sha256": sha("x: 1\n")})
    assert list(outside.iterdir()) == []  # nothing written through the link, the save still worked
    assert (folder / "a.yml").read_text() == "x: 2\n"
    assert safefs and os


# --- through the hub ------------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path, folder, caplog):
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
    return client, admin, node, tmp_path, folder


def url(node, tail):
    return f"/api/v1/nodes/{node}/traefik/{tail}"


def body(**over):
    return {
        "spec": {
            "name": "blog",
            "hosts": ["blog.example.com"],
            "container": "blog-web",
            "port": 80,
            **over,
        }
    }


def test_create_pause_resume_rename_delete_and_audit(hub):
    client, admin, node, tmp, folder = hub
    created = client.post(url(node, "sites"), headers=admin, json=body())
    assert created.status_code == 201
    on_disk = (folder / "sites" / "blog.yml").read_text()
    assert sitebuilder.untouched(on_disk) is not None and (folder / "reload.txt").exists()
    listed = client.get(url(node, "managed"), headers=admin).json()["sites"]
    assert (
        listed[0]["name"] == "blog"
        and listed[0]["paused"] is False
        and listed[0]["modified_by_hand"] is False
    )

    assert client.post(url(node, "sites/blog/pause"), headers=admin).json() == {"done": True}
    assert client.get(url(node, "managed"), headers=admin).json()["sites"][0]["paused"] is True
    # the site is gone for Traefik: it is no longer among the routers of the files
    assert client.get(url(node, "sites"), headers=admin).json()["routers"] == []
    assert client.post(url(node, "sites/blog/resume"), headers=admin).status_code == 200
    assert [r["name"] for r in client.get(url(node, "sites"), headers=admin).json()["routers"]] == [
        "blog"
    ]

    sha_now = client.get(url(node, "managed"), headers=admin).json()["sites"][0]["sha256"]
    renamed = client.post(
        url(node, "sites/blog/rename"),
        headers=admin,
        json={"new_name": "journal", "expected_sha256": sha_now},
    )
    assert renamed.status_code == 200
    names = [r["name"] for r in client.get(url(node, "sites"), headers=admin).json()["routers"]]
    assert names == ["journal"] and not (folder / "sites" / "blog.yml").exists()

    sha_now = renamed.json()["sha256"]
    assert (
        client.delete(
            url(node, f"sites/journal?expected_sha256={sha_now}"), headers=admin
        ).status_code
        == 200
    )
    assert client.get(url(node, "managed"), headers=admin).json()["sites"] == []
    events = [
        r[0]
        for r in sqlite3.connect(tmp / "data" / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'traefik.site_%' ORDER BY id"
        )
    ]
    assert events == [
        "traefik.site_create",
        "traefik.site_pause",
        "traefik.site_resume",
        "traefik.site_rename",
        "traefik.site_delete",
    ]


def test_a_taken_name_is_refused_and_preview_says_so(hub):
    client, admin, node, tmp, folder = hub
    (folder / "old.yml").write_text(
        yaml.safe_dump(
            {"http": {"routers": {"blog": {"rule": "Host(`x.example.com`)", "service": "s"}}}}
        )
    )
    preview = client.post(url(node, "sites/preview"), headers=admin, json=body())
    assert preview.status_code == 200 and preview.json()["name_taken"] is True
    assert "Host(`blog.example.com`)" in preview.json()["yaml"]
    refused = client.post(url(node, "sites"), headers=admin, json=body())
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "traefik.name_taken"
    assert not (folder / "sites").exists()
    client.post(url(node, "sites"), headers=admin, json=body(name="other")) if False else None


def test_a_site_edited_by_hand_is_not_overwritten_by_the_form(hub):
    client, admin, node, tmp, folder = hub
    client.post(url(node, "sites"), headers=admin, json=body())
    path = folder / "sites" / "blog.yml"
    path.write_text(path.read_text() + "# added by hand\n")
    managed = client.get(url(node, "managed"), headers=admin).json()["sites"][0]
    assert managed["modified_by_hand"] is True
    changed = client.put(
        url(node, "sites/blog"),
        headers=admin,
        json={**body(hosts=["new.example.com"]), "expected_sha256": managed["sha256"]},
    )
    assert (
        changed.status_code == 409 and changed.json()["error"]["code"] == "traefik.modified_by_hand"
    )
    assert "added by hand" in path.read_text()


def test_updating_a_site_checks_the_version_and_keeps_history(hub):
    client, admin, node, tmp, folder = hub
    client.post(url(node, "sites"), headers=admin, json=body())
    current = client.get(url(node, "managed"), headers=admin).json()["sites"][0]["sha256"]
    stale = client.put(
        url(node, "sites/blog"), headers=admin, json={**body(), "expected_sha256": "0" * 64}
    )
    assert stale.status_code == 409
    ok = client.put(
        url(node, "sites/blog"),
        headers=admin,
        json={**body(hosts=["new.example.com"]), "expected_sha256": current},
    )
    assert ok.status_code == 200
    assert "new.example.com" in (folder / "sites" / "blog.yml").read_text()
    versions = client.get(url(node, "history/sites/blog.yml"), headers=admin).json()["versions"]
    assert len(versions) == 1
    restored = client.post(
        url(node, "restore/sites/blog.yml"),
        headers=admin,
        json={"version": versions[0]["version"], "expected_sha256": ok.json()["sha256"]},
    )
    assert restored.status_code == 200
    assert "blog.example.com" in (folder / "sites" / "blog.yml").read_text()
    assert (
        len(client.get(url(node, "history/sites/blog.yml"), headers=admin).json()["versions"]) == 2
    )


def test_name_in_the_path_must_match_and_paused_sites_are_not_edited(hub):
    client, admin, node, tmp, folder = hub
    client.post(url(node, "sites"), headers=admin, json=body())
    sha_now = client.get(url(node, "managed"), headers=admin).json()["sites"][0]["sha256"]
    mismatch = client.put(
        url(node, "sites/blog"),
        headers=admin,
        json={**body(name="other"), "expected_sha256": sha_now},
    )
    assert mismatch.status_code == 422
    client.post(url(node, "sites/blog/pause"), headers=admin)
    paused = client.put(
        url(node, "sites/blog"), headers=admin, json={**body(), "expected_sha256": sha_now}
    )
    assert paused.status_code == 409 and paused.json()["error"]["code"] == "traefik.site_paused"


def test_bad_requests_are_refused(hub):
    client, admin, node, tmp, folder = hub
    assert (
        client.post(
            url(node, "sites"), headers=admin, json=body(hosts=["a.example.com`) || Host(`x.org"])
        ).status_code
        == 422
    )
    assert (
        client.post(
            url(node, "sites"), headers=admin, json={"spec": {**body()["spec"], "extra": 1}}
        ).status_code
        == 422
    )
    assert client.post(url(node, "sites/Bad_Name/pause"), headers=admin).status_code == 422
    assert (
        client.delete(url(node, "sites/blog"), headers=admin).status_code == 422
    )  # needs the version
    assert client.get(url(node, "history/../x.yml"), headers=admin).status_code in (404, 422)
    assert (
        client.post(
            url(node, "restore/app.yml"), headers=admin, json={"version": "../x"}
        ).status_code
        == 422
    )
    assert not (folder / "sites").exists()


def test_rights_creating_sites_is_sensitive_and_hidden_nodes_are_not_found(hub):
    client, admin, node, tmp, folder = hub
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    assert client.post(url(node, "sites"), headers=bob, json=body()).status_code == 404
    for right in ("nodes.read", "traefik.read", "traefik.files.read", "traefik.files.write"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    refused = client.post(url(node, "sites"), headers=bob, json=body())
    assert refused.status_code == 403  # editing raw files does not allow building sites
    assert client.get(url(node, "managed"), headers=bob).status_code == 200
    key = client.post(
        "/api/v1/apikeys",
        headers=admin,
        json={"name": "ci", "rights": ["traefik.sites.write", "traefik.read", "nodes.read"]},
    )
    assert key.status_code == 422
    assert json  # (json import kept for debugging failures)


# --- findings of the security review ---------------------------------------------------------


def test_the_generic_file_editor_cannot_write_into_the_sites_folder_without_the_sites_right(hub):
    client, admin, node, tmp, folder = hub
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    for right in ("nodes.read", "traefik.read", "traefik.files.read", "traefik.files.write"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    evil = "http:\n  routers:\n    v:\n      rule: Host(`victim.example.com`)\n      service: s\n"
    put = client.put(
        url(node, "files/sites/evil.yml"),
        headers=bob,
        json={"content": evil, "expected_sha256": None},
    )
    assert put.status_code == 403 and not (folder / "sites").exists()
    elsewhere = client.put(
        url(node, "files/other/ok.yml"),
        headers=bob,
        json={"content": "a: 1\n", "expected_sha256": None},
    )
    assert (
        elsewhere.status_code == 404 or elsewhere.status_code == 200
    )  # a missing folder, not a right
    restore = client.post(
        url(node, "restore/sites/x.yml"), headers=bob, json={"version": "1" * 13 + "." + "a" * 8}
    )
    assert restore.status_code == 403
    # With the sites right as well, the same call goes through.
    client.post(
        f"/api/v1/users/{uid}/grants",
        headers=admin,
        json={"right": "traefik.sites.write", "scope": f"node:{node}"},
    )
    (folder / "sites").mkdir()
    allowed = client.put(
        url(node, "files/sites/evil.yml"),
        headers=bob,
        json={"content": "a: 1\n", "expected_sha256": None},
    )
    assert allowed.status_code == 200


def test_a_deeply_nested_spec_line_does_not_break_the_listing(hub):
    client, admin, node, tmp, folder = hub
    client.post(url(node, "sites"), headers=admin, json=body())
    bomb = "# armadok-site: " + "[" * 50000 + "\nhttp: {}\n"
    (folder / "sites" / "bomb.yml").write_text(bomb)
    got = client.get(url(node, "managed"), headers=admin)
    assert got.status_code == 200
    by_name = {s["name"]: s for s in got.json()["sites"]}
    assert by_name["bomb"]["spec"] is None and by_name["bomb"]["modified_by_hand"] is True
    assert by_name["blog"]["modified_by_hand"] is False
    assert sitebuilder.spec_of("# armadok-site: " + "[" * 50000) is None
    assert sitebuilder.spec_of("# armadok-site: " + "x" * 5000) is None


def test_names_hidden_by_truncation_make_creation_refuse(hub, monkeypatch):
    client, admin, node, tmp, folder = hub
    from armadok.worker import traefik_ops

    monkeypatch.setattr(traefik_ops, "SNAPSHOT_BUDGET_BYTES", 10)
    (folder / "big.yml").write_text("http:\n  routers: {}\n" + "# padding\n" * 100)
    refused = client.post(url(node, "sites"), headers=admin, json=body())
    assert (
        refused.status_code == 409
        and refused.json()["error"]["code"] == "traefik.snapshot_incomplete"
    )
    assert not (folder / "sites").exists()


def test_a_paused_site_still_reserves_its_router_names(hub):
    client, admin, node, tmp, folder = hub
    (folder / "sites").mkdir()
    (folder / "sites" / "hand.yml.paused").write_text(
        yaml.safe_dump(
            {"http": {"routers": {"blog": {"rule": "Host(`a.example.com`)", "service": "x"}}}}
        )
    )
    refused = client.post(url(node, "sites"), headers=admin, json=body())
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "traefik.name_taken"


def test_a_failed_rename_leaves_one_live_site(hub, monkeypatch):
    client, admin, node, tmp, folder = hub
    client.post(url(node, "sites"), headers=admin, json=body())
    sha_now = client.get(url(node, "managed"), headers=admin).json()["sites"][0]["sha256"]
    real = sites_ops.site_delete
    calls = []

    def flaky(args):
        calls.append(args["name"])
        if args["name"] == "blog":
            raise OperationError("worker.conflict")
        return real(args)

    from armadok.worker import ops

    monkeypatch.setitem(ops.OPERATIONS, "traefik.site.delete", flaky)
    failed = client.post(
        url(node, "sites/blog/rename"),
        headers=admin,
        json={"new_name": "journal", "expected_sha256": sha_now},
    )
    assert failed.status_code == 409
    assert (folder / "sites" / "blog.yml").exists() and not (
        folder / "sites" / "journal.yml"
    ).exists()
    assert calls == ["blog", "journal"]


def test_the_number_of_sites_and_of_kept_versions_is_bounded(folder, monkeypatch):
    monkeypatch.setattr(sites_ops, "MAX_SITES", 3)
    for i in range(3):
        sites_ops.site_write({"name": f"s{i}", "content": "a: 1\n", "expected_sha256": None})
    with pytest.raises(OperationError) as many:
        sites_ops.site_write({"name": "s9", "content": "a: 1\n", "expected_sha256": None})
    assert many.value.code == "worker.too_large"
    monkeypatch.setattr(history, "MAX_TOTAL", 5)
    for i in range(10):
        sites_ops.site_write(
            {
                "name": f"s{i % 3}",
                "content": f"v: {i}\n",
                "expected_sha256": sha("a: 1\n") if i < 3 else sha(f"v: {i - 3}\n"),
            }
        )
    kept = list((folder / ".armadok-history").iterdir())
    assert len(kept) <= 5


def test_resume_removes_a_planted_link_instead_of_keeping_it(folder, tmp_path, monkeypatch):
    sites_ops.site_write({"name": "blog", "content": "a: 1\n", "expected_sha256": None})
    sites_ops.site_pause({"name": "blog"})
    secret = tmp_path / "secret"
    secret.write_text("s")
    real_link = os.link

    def swap(src, dst, **kw):
        real_link(src, dst, **kw)
        os.unlink(dst, dir_fd=kw["dst_dir_fd"])
        os.symlink(secret, dst, dir_fd=kw["dst_dir_fd"])

    monkeypatch.setattr(sites_ops.os, "link", swap)
    with pytest.raises(OperationError):
        sites_ops.site_resume({"name": "blog"})
    assert not (folder / "sites" / "blog.yml").exists() and secret.read_text() == "s"


def test_a_lying_worker_answer_is_refused(hub, monkeypatch):
    client, admin, node, tmp, folder = hub
    from armadok.worker import ops

    monkeypatch.setitem(
        ops.OPERATIONS, "traefik.site.write", lambda a: {"sha256": "0" * 64, "size": 1}
    )
    bad = client.post(url(node, "sites"), headers=admin, json=body())
    assert bad.status_code == 502 and bad.json()["error"]["code"] == "worker.invalid_answer"
