import hashlib
import os

import pytest

from armadok.modules.traefik import parse
from armadok.worker import traefik_ops as ops
from armadok.worker.operation import OperationError
from tests.test_docker import PASSWORD, connect_worker, fake_docker, live  # noqa: F401

SITE = """\
http:
  routers:
    blog:
      rule: Host(`blog.example.com`) || Host(`www.example.com`)
      service: blog-svc
      entryPoints: [websecure]
      middlewares: [auth, headers@file, sso@docker]
      tls:
        certResolver: letsencrypt
  services:
    blog-svc:
      loadBalancer:
        servers:
          - url: http://web:80
  middlewares:
    auth:
      basicAuth:
        users: ["admin:$apr1$x"]
    headers:
      headers: {frameDeny: true}
"""
SHA = hashlib.sha256(SITE.encode()).hexdigest()


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "dynamic"
    (folder / "apps").mkdir(parents=True)
    (folder / "apps" / "blog.yml").write_text(SITE)
    (folder / "base.yaml").write_text("http: {}\n")
    (folder / "notes.txt").write_text("not a config file")
    monkeypatch.setenv("ARMADOK_TRAEFIK_DIR", str(folder))
    return folder


def code_of(call, *args):
    with pytest.raises(OperationError) as caught:
        call(*args)
    return caught.value.code


# --- the worker: confinement and files -------------------------------------------------------


def test_list_finds_yaml_files_in_subfolders_only(root):
    paths = [f["path"] for f in ops.files_list({})["files"]]
    assert paths == ["apps/blog.yml", "base.yaml"]


@pytest.mark.parametrize(
    "path",
    [
        "../x.yml",
        "apps/../blog.yml",
        "/etc/passwd",
        "apps//blog.yml",
        "a/b/c/d/e/f.yml",
        "blog.txt",
        "apps/blog.yml/",
        "",
        None,
        5,
        "apps/.hidden.yml",
        "reload.txt",
    ],
)
def test_paths_that_could_escape_or_are_not_config_files_are_refused(root, path):
    assert code_of(ops.file_read, {"path": path}) == "worker.invalid_path"


def test_symlinked_folders_and_files_are_never_followed(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.yml").write_text("http: {leak: 1}\n")
    os.symlink(outside, root / "linked")
    os.symlink(outside / "leak.yml", root / "apps" / "link.yml")
    assert code_of(ops.file_read, {"path": "linked/leak.yml"}) == "worker.forbidden"
    assert code_of(ops.file_read, {"path": "apps/link.yml"}) == "worker.forbidden"
    paths = [f["path"] for f in ops.files_list({})["files"]]
    assert not any("linked" in p or "link.yml" in p for p in paths)


def test_save_in_a_subfolder_refreshes_reload_file_but_root_saves_do_not(root):
    (root / "base.yaml").write_text("http: {}\n")
    base_sha = hashlib.sha256(b"http: {}\n").hexdigest()
    ops.file_write({"path": "base.yaml", "content": "http: {a: 1}\n", "expected_sha256": base_sha})
    assert not (root / "reload.txt").exists()
    ops.file_write(
        {"path": "apps/blog.yml", "content": SITE + "# edited\n", "expected_sha256": SHA}
    )
    assert (root / "reload.txt").exists()


def test_conflicts_are_detected_and_nothing_is_overwritten(root):
    args = {"path": "apps/blog.yml", "content": "http: {}\n"}
    assert code_of(ops.file_write, {**args, "expected_sha256": "0" * 64}) == "worker.conflict"
    assert code_of(ops.file_write, {**args, "expected_sha256": None}) == "worker.conflict"
    assert (root / "apps" / "blog.yml").read_text() == SITE


def test_snapshot_returns_all_files_in_one_answer(root):
    snap = ops.snapshot({})
    assert sorted(f["path"] for f in snap["files"]) == ["apps/blog.yml", "base.yaml"]
    assert snap["truncated"] is False


def test_unconfigured_folder_gives_a_clean_code(monkeypatch):
    monkeypatch.delenv("ARMADOK_TRAEFIK_DIR", raising=False)
    assert code_of(ops.files_list, {}) == "worker.traefik_not_configured"


# --- the hub-side YAML reading ---------------------------------------------------------------


def test_summary_reads_routers_services_middlewares_with_traefik_names():
    out = parse.summarize("apps/blog.yml", SITE)
    router = out["routers"][0]
    assert router["id"] == "blog@file" and router["service"] == "blog-svc@file"
    assert router["middlewares"] == ["auth@file", "headers@file", "sso@docker"]
    assert router["tls"] is True and router["cert_resolver"] == "letsencrypt"
    assert parse.hosts_of(router["rule"]) == ["blog.example.com", "www.example.com"]
    assert out["services"][0]["urls"] == ["http://web:80"]
    assert {(m["id"], m["type"]) for m in out["middlewares"]} == {
        ("auth@file", "basicAuth"),
        ("headers@file", "headers"),
    }


def test_yaml_aliases_and_garbage_are_refused_without_crashing():
    bomb = "a: &a [1,2,3]\nb: &b [*a,*a,*a]\nc: [*b,*b,*b]\n"
    with pytest.raises(parse.YamlError):
        parse.load(bomb)
    for bad in ("a: [unclosed", "!!python/object/apply:os.system ['id']", "\t- bad"):
        with pytest.raises(parse.YamlError):
            parse.load(bad)
    assert parse.summarize("x.yml", "a: [unclosed")["error"] == "invalid_yaml"
    odd = parse.summarize(
        "x.yml", "http: {routers: {a: 5, b: {rule: 7, middlewares: nope}}, services: [1]}"
    )
    assert odd["error"] is None and odd["routers"][0]["rule"] == ""


# --- through the hub -------------------------------------------------------------------------


def test_hub_lists_reads_saves_validates_audits_and_summarises(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "full")
    base = f"/api/v1/nodes/{node}/traefik"
    assert [f["path"] for f in client.get(f"{base}/files", headers=headers).json()["files"]] == [
        "apps/blog.yml",
        "base.yaml",
    ]
    read = client.get(f"{base}/files/apps/blog.yml", headers=headers).json()
    assert read["content"] == SITE
    sites = client.get(f"{base}/sites", headers=headers).json()
    assert sites["routers"][0]["id"] == "blog@file" and sites["errors"] == []

    broken = client.put(
        f"{base}/files/apps/blog.yml",
        headers=headers,
        json={"content": "a: [oops", "expected_sha256": read["sha256"]},
    )
    assert broken.status_code == 422 and broken.json()["error"]["code"] == "traefik.invalid_yaml"
    assert (root / "apps" / "blog.yml").read_text() == SITE  # a broken file never reaches the disk
    ok = client.put(
        f"{base}/files/apps/blog.yml",
        headers=headers,
        json={"content": SITE + "# edited\n", "expected_sha256": read["sha256"]},
    )
    assert ok.status_code == 200
    stale = client.put(
        f"{base}/files/apps/blog.yml",
        headers=headers,
        json={"content": "http: {}\n", "expected_sha256": read["sha256"]},
    )
    assert stale.status_code == 409
    assert client.get(f"{base}/files/../x.yml", headers=headers).status_code in (404, 422)
    assert client.get(f"{base}/files/notes.txt", headers=headers).status_code == 422

    import sqlite3

    rows = (
        sqlite3.connect(tmp_path / "armadok.db")
        .execute(
            "SELECT event, outcome, details FROM audit_log WHERE event LIKE 'traefik.%' ORDER BY id"
        )
        .fetchall()
    )
    assert [(r[0], r[1]) for r in rows] == [
        ("traefik.file_read", "success"),
        ("traefik.file_write", "success"),
        ("traefik.file_write", "failure"),
    ]
    assert "$apr1" not in str(rows) and "blog.example.com" not in str(rows)
    stop.set()


def test_relations_link_routers_services_containers_domains_and_middlewares(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "full")
    graph = client.get(f"/api/v1/nodes/{node}/relations", headers=headers).json()
    by_label = {(o["type"], o["label"]): o for o in graph["objects"]}
    assert ("router", "blog@file") in by_label and ("domain", "blog.example.com") in by_label
    assert ("middleware", "auth@file") in by_label and ("service", "blog-svc@file") in by_label
    kinds = {(k["source"]["type"], k["kind"], k["target"]["type"]) for k in graph["links"]}
    assert {
        ("router", "routes_to", "service"),
        ("router", "uses", "middleware"),
        ("router", "serves", "domain"),
        ("service", "targets", "container"),
    } <= kinds
    # The service points at the real container "web" that the Docker provider found.
    web_id = next(
        o["id"] for o in graph["objects"] if o["type"] == "container" and o["label"] == "web"
    )
    targets = [k for k in graph["links"] if k["kind"] == "targets"]
    assert targets[0]["target"]["id"] == web_id
    stop.set()


def test_hub_rights_and_node_type(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    docker_only, stop1 = connect_worker(client, headers, port, tmp_path, "docker")
    refused = client.get(f"/api/v1/nodes/{docker_only}/traefik/files", headers=headers)
    assert (
        refused.status_code == 409
        and refused.json()["error"]["code"] == "worker.op_not_for_node_type"
    )
    node, stop2 = connect_worker(client, headers, port, tmp_path, "full")
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob_token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {bob_token}"}
    url = f"/api/v1/nodes/{node}/traefik/files"
    assert client.get(url, headers=bob).status_code == 404
    for right in ("nodes.read", "traefik.read"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=headers,
            json={"right": right, "scope": f"node:{node}"},
        )
    assert client.get(url, headers=bob).status_code == 200
    assert client.get(f"{url}/apps/blog.yml", headers=bob).status_code == 403
    assert (
        client.put(f"{url}/apps/blog.yml", headers=bob, json={"content": "http: {}\n"}).status_code
        == 403
    )
    stop1.set()
    stop2.set()


# --- findings of the security review ---------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "a: 2001-02-30\n",  # looks like a date but is not one: used to crash with ValueError
        "a: " + "9" * 5000 + "\n",  # number too long to convert
        "a: !!int x\n",
        "a: !!float y\n",
        "a: [" * 500 + "1" + "]" * 500 + "\n",  # deep nesting
        "http: {routers: {r: {rule: 2001-02-30}}}\n",
    ],
)
def test_hostile_yaml_never_crashes_the_summary_or_the_validator(hostile):
    # Either it parses as plain data or it is refused cleanly: never an unhandled exception.
    try:
        parse.load(hostile)
    except parse.YamlError:
        pass
    result = parse.summarize("x.yml", hostile)
    assert result["error"] in (None, "invalid_yaml")


def test_service_urls_are_reduced_to_host_and_port_so_credentials_do_not_leak():
    text = (
        "http:\n  services:\n    s:\n      loadBalancer:\n        servers:\n"
        "          - url: http://user:s3cret@backend:8080/path?token=abc\n"
        "          - url: http://[\n"
    )
    out = parse.summarize("x.yml", text)
    assert out["services"][0]["urls"] == ["http://backend:8080"]
    assert "s3cret" not in str(out) and "token" not in str(out)


def test_one_bad_file_does_not_hide_the_others():
    good = parse.summarize("a.yml", SITE)
    bad = parse.summarize("b.yml", "a: [oops")
    assert good["error"] is None and bad["error"] == "invalid_yaml"


def test_reload_file_planted_as_a_hard_link_is_not_emptied(root, tmp_path):
    victim = tmp_path / "precious.txt"
    victim.write_text("do not empty me")
    os.link(victim, root / "reload.txt")
    ops.file_write({"path": "apps/blog.yml", "content": SITE + "# x\n", "expected_sha256": SHA})
    assert victim.read_text() == "do not empty me"


def test_a_huge_tree_is_cut_short_and_reported(root, monkeypatch):
    monkeypatch.setattr(ops, "MAX_ENTRIES_SCANNED", 5)
    for n in range(20):
        (root / f"f{n:02}.yml").write_text("http: {}\n")
    out = ops.files_list({})
    assert out["truncated"] is True and len(out["files"]) <= 5


def test_a_viewer_with_only_traefik_read_sees_no_containers(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "full")
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    for right in ("nodes.read", "traefik.read"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=headers,
            json={"right": right, "scope": f"node:{node}"},
        )
    graph = client.get(f"/api/v1/nodes/{node}/relations", headers=bob).json()
    assert not any(o["type"] == "container" for o in graph["objects"])
    assert ("service", "targets") not in {(k["source"]["type"], k["kind"]) for k in graph["links"]}
    assert any(o["type"] == "router" for o in graph["objects"])  # Traefik's own objects remain
    stop.set()
