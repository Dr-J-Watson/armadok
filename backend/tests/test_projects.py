import hashlib
import os
import stat
import threading

import pytest

from armadok.worker import projects_ops as ops
from armadok.worker.operation import OperationError
from tests.test_docker import PASSWORD, connect_worker, fake_docker, live  # noqa: F401

COMPOSE = "services:\n  web:\n    image: nginx:1.27\n"
SHA = hashlib.sha256(COMPOSE.encode()).hexdigest()


@pytest.fixture
def root(tmp_path, monkeypatch):
    stacks = tmp_path / "stacks"
    (stacks / "blog").mkdir(parents=True)
    (stacks / "blog" / "compose.yaml").write_text(COMPOSE)
    (stacks / "blog" / "docker-compose.prod.yml").write_text("services: {}\n")
    (stacks / "blog" / ".env").write_text("SECRET=hunter2\n")
    (stacks / "notes").mkdir()
    (stacks / "notes" / "readme.txt").write_text("no compose here")
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(stacks))
    return stacks


def code_of(call, *args):
    with pytest.raises(OperationError) as caught:
        call(*args)
    return caught.value.code


# --- the worker: what it may touch ---------------------------------------------------------


def test_list_shows_projects_with_compose_files_only(root):
    out = ops.projects_list({})
    assert out["truncated"] is False and len(out["projects"]) == 1
    blog = out["projects"][0]
    assert blog["name"] == "blog" and blog["files"] == ["compose.yaml", "docker-compose.prod.yml"]
    # What the import screen shows: the services of the main file, `.env` and `.git`, no error.
    assert blog["services"] == ["web"] and blog["has_env"] is True and blog["has_git"] is False
    assert blog["error"] is None


def test_a_broken_compose_file_is_reported_by_a_code_not_a_failure(root):
    (root / "blog" / "compose.yaml").write_text("services: [unclosed\n")
    assert ops.projects_list({})["projects"][0]["error"] == "invalid_yaml"


def test_read_returns_text_and_version_hash_but_never_the_env_file(root):
    out = ops.file_read({"project": "blog", "file": "compose.yaml"})
    assert out["content"] == COMPOSE and out["sha256"] == SHA
    assert code_of(ops.file_read, {"project": "blog", "file": ".env"}) == "worker.invalid_path"


@pytest.mark.parametrize(
    "project,file",
    [
        ("..", "compose.yaml"),
        ("blog/../blog", "compose.yaml"),
        ("../stacks/blog", "compose.yaml"),
        ("/etc", "compose.yaml"),
        ("blog", "../compose.yaml"),
        ("blog", "sub/compose.yaml"),
        ("blog", "compose.yaml\x00.txt"),
        ("blog", "compose.yaml/"),
        ("blog", "passwd"),
        ("", "compose.yaml"),
        (None, "compose.yaml"),
        ("blog", None),
        ("blog", ["compose.yaml"]),
    ],
)
def test_paths_that_could_escape_are_refused_before_touching_the_disk(root, project, file):
    assert code_of(ops.file_read, {"project": project, "file": file}) in (
        "worker.invalid_path",
        "worker.not_found",
    )


def test_symlinks_are_never_followed(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "compose.yaml").write_text("services: {leak: {}}\n")
    os.symlink(outside, root / "linked")  # a project folder that is a symlink
    os.symlink(tmp_path / "outside" / "compose.yaml", root / "blog" / "compose.override.yml")
    assert (
        code_of(ops.file_read, {"project": "linked", "file": "compose.yaml"}) == "worker.forbidden"
    )
    assert (
        code_of(ops.file_read, {"project": "blog", "file": "compose.override.yml"})
        == "worker.forbidden"
    )
    write_args = {
        "project": "blog",
        "file": "compose.override.yml",
        "content": "x",
        "expected_sha256": None,
    }
    assert code_of(ops.file_write, write_args) in ("worker.forbidden", "worker.conflict")
    assert (outside / "compose.yaml").read_text() == "services: {leak: {}}\n"
    names = [p["name"] for p in ops.projects_list({})["projects"]]
    assert "linked" not in names
    assert "compose.override.yml" not in next(p for p in ops.projects_list({})["projects"])["files"]


def test_write_needs_the_version_it_replaces_and_is_atomic(root):
    args = {"project": "blog", "file": "compose.yaml", "content": "services: {}\n"}
    assert code_of(ops.file_write, {**args, "expected_sha256": "0" * 64}) == "worker.conflict"
    assert code_of(ops.file_write, {**args, "expected_sha256": None}) == "worker.conflict"
    assert (root / "blog" / "compose.yaml").read_text() == COMPOSE  # untouched by refusals
    saved = ops.file_write({**args, "expected_sha256": SHA})
    assert saved["sha256"] == hashlib.sha256(b"services: {}\n").hexdigest()
    assert (root / "blog" / "compose.yaml").read_text() == "services: {}\n"
    assert not list((root / "blog").glob("*.tmp"))  # no leftover temporary file
    # The same stale version cannot be used twice: someone else's change is never overwritten.
    assert code_of(ops.file_write, {**args, "expected_sha256": SHA}) == "worker.conflict"


def test_write_creates_a_new_compose_file_only_when_told_it_does_not_exist(root):
    new = {"project": "blog", "file": "compose.dev.yaml", "content": "services: {}\n"}
    assert code_of(ops.file_write, {**new, "expected_sha256": "a" * 64}) == "worker.conflict"
    ops.file_write({**new, "expected_sha256": None})
    assert (root / "blog" / "compose.dev.yaml").exists()


def test_write_keeps_file_permissions_and_limits_size(root):
    path = root / "blog" / "compose.yaml"
    path.chmod(0o600)
    ops.file_write(
        {"project": "blog", "file": "compose.yaml", "content": "a: 1\n", "expected_sha256": SHA}
    )
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    big = "x" * (ops.MAX_FILE_BYTES + 1)
    assert (
        code_of(
            ops.file_write,
            {"project": "blog", "file": "compose.yaml", "content": big, "expected_sha256": None},
        )
        == "worker.too_large"
    )


def test_binary_and_unconfigured_cases_give_clean_codes(root, monkeypatch):
    (root / "blog" / "compose.bin.yaml").write_bytes(b"\xff\xfe\x00")
    assert (
        code_of(ops.file_read, {"project": "blog", "file": "compose.bin.yaml"}) == "worker.not_text"
    )
    monkeypatch.delenv("ARMADOK_PROJECTS_DIR")
    assert code_of(ops.projects_list, {}) == "worker.projects_not_configured"


# --- through the hub -----------------------------------------------------------------------


def login(client, user, password=PASSWORD):
    token = client.post("/api/v1/auth/login", json={"username": user, "password": password}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_hub_lists_reads_saves_and_audits_without_logging_content(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    base = f"/api/v1/nodes/{node}/projects"
    assert client.get(base, headers=headers).json()["projects"] == []  # not imported yet
    assert (
        client.post(f"{base}/import", headers=headers, json={"projects": ["blog"]}).status_code
        == 200
    )
    assert client.get(base, headers=headers).json()["projects"][0]["name"] == "blog"
    read = client.get(f"{base}/blog/files/compose.yaml", headers=headers).json()
    assert read["content"] == COMPOSE
    ok = client.put(
        f"{base}/blog/files/compose.yaml",
        headers=headers,
        json={"content": "services: {}\n", "expected_sha256": read["sha256"]},
    )
    assert ok.status_code == 200
    stale = client.put(
        f"{base}/blog/files/compose.yaml",
        headers=headers,
        json={"content": "other\n", "expected_sha256": read["sha256"]},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "worker.conflict"
    import sqlite3

    rows = (
        sqlite3.connect(tmp_path / "armadok.db")
        .execute("SELECT event, details FROM audit_log WHERE event LIKE 'project.%'")
        .fetchall()
    )
    # The refused (conflicting) save is recorded too, with its outcome and code but no content.
    assert [r[0] for r in rows] == [
        "project.import",
        "project.file_read",
        "project.file_write",
        "project.file_write",
    ]
    outcomes = (
        sqlite3.connect(tmp_path / "armadok.db")
        .execute("SELECT outcome FROM audit_log WHERE event = 'project.file_write' ORDER BY id")
        .fetchall()
    )
    assert [o[0] for o in outcomes] == ["success", "failure"]
    assert "worker.conflict" in rows[3][1]
    assert "services" not in str(rows) and "nginx" not in str(rows)
    stop.set()


def test_hub_refuses_bad_names_and_enforces_rights(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    base = f"/api/v1/nodes/{node}/projects"
    for bad in ("..", "%2e%2e", "a b", "x" * 80):
        assert client.get(f"{base}/{bad}/files/compose.yaml", headers=headers).status_code in (
            404,
            422,
        )
    assert client.get(f"{base}/blog/files/.env", headers=headers).status_code in (404, 422)
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    bob = login(client, "bob")
    assert client.get(base, headers=bob).status_code == 404  # cannot see the node
    for right in ("nodes.read", "projects.read"):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=headers,
            json={"right": right, "scope": f"node:{node}"},
        )
    assert client.get(base, headers=bob).status_code == 200
    assert client.get(f"{base}/blog/files/compose.yaml", headers=bob).status_code == 403
    assert (
        client.put(
            f"{base}/blog/files/compose.yaml", headers=bob, json={"content": "x"}
        ).status_code
        == 403
    )
    stop.set()


# --- findings of the security review --------------------------------------------------------


def test_a_fifo_named_like_a_compose_file_does_not_block_the_worker(root):
    os.mkfifo(root / "blog" / "compose.fifo.yaml")
    done = []
    thread = threading.Thread(
        target=lambda: done.append(
            code_of(ops.file_read, {"project": "blog", "file": "compose.fifo.yaml"})
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5)
    assert done == ["worker.forbidden"], "reading a FIFO must be refused at once, not hang"
    assert "compose.fifo.yaml" not in next(p for p in ops.projects_list({})["projects"])["files"]


def test_hard_links_and_directories_named_like_compose_files_are_refused(root, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("other data")
    os.link(outside, root / "blog" / "compose.linked.yaml")
    assert (
        code_of(ops.file_read, {"project": "blog", "file": "compose.linked.yaml"})
        == "worker.forbidden"
    )
    (root / "blog" / "compose.dir.yaml").mkdir()
    assert (
        code_of(ops.file_read, {"project": "blog", "file": "compose.dir.yaml"})
        == "worker.forbidden"
    )


def test_creating_a_file_never_replaces_one_that_appeared_meanwhile(root, monkeypatch):
    target = root / "blog" / "compose.new.yaml"
    real_link = os.link

    def racing_link(src, dst, **kwargs):
        target.write_text("someone else wrote this first\n")  # appears just before we publish
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    args = {
        "project": "blog",
        "file": "compose.new.yaml",
        "content": "mine\n",
        "expected_sha256": None,
    }
    assert code_of(ops.file_write, args) == "worker.conflict"
    assert target.read_text() == "someone else wrote this first\n"
    assert not [f for f in os.listdir(root / "blog") if f.endswith(".tmp")]


def test_a_file_edited_elsewhere_while_saving_is_not_overwritten(root, monkeypatch):
    target = root / "blog" / "compose.yaml"
    real_fsync = os.fsync
    state = {"done": False}

    def edit_during_write(fd):
        real_fsync(fd)
        if not state["done"]:
            state["done"] = True
            target.write_text("services: {edited: elsewhere}\n")  # changes under our feet

    monkeypatch.setattr(os, "fsync", edit_during_write)
    args = {
        "project": "blog",
        "file": "compose.yaml",
        "content": "services: {}\n",
        "expected_sha256": SHA,
    }
    assert code_of(ops.file_write, args) == "worker.conflict"
    assert target.read_text() == "services: {edited: elsewhere}\n"


def test_a_leftover_temp_file_never_blocks_saving_and_stale_ones_are_removed(root):
    stale = root / "blog" / ".compose.yaml.armadok-0123456789abcdef.tmp"
    stale.write_text("crashed write")
    old = stale.stat().st_mtime - 3600
    os.utime(stale, (old, old))
    ops.file_write(
        {"project": "blog", "file": "compose.yaml", "content": "a: 1\n", "expected_sha256": SHA}
    )
    assert not stale.exists()


def test_new_files_are_not_world_readable_and_special_bits_are_not_copied(root):
    ops.file_write(
        {
            "project": "blog",
            "file": "compose.dev.yaml",
            "content": "a: 1\n",
            "expected_sha256": None,
        }
    )
    assert stat.S_IMODE((root / "blog" / "compose.dev.yaml").stat().st_mode) == 0o640
    path = root / "blog" / "compose.yaml"
    path.chmod(0o4644)  # setuid bit set on the original
    ops.file_write(
        {"project": "blog", "file": "compose.yaml", "content": "b: 2\n", "expected_sha256": SHA}
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_the_projects_folder_must_be_a_real_absolute_folder_not_the_filesystem_root(monkeypatch):
    for bad in ("/", "relative/path", "/does/not/exist"):
        monkeypatch.setenv("ARMADOK_PROJECTS_DIR", bad)
        assert code_of(ops.projects_list, {}) == "worker.projects_not_configured"


def test_an_unreadable_project_does_not_break_the_list(root):
    (root / "locked").mkdir()
    (root / "locked" / "compose.yaml").write_text("a: 1\n")
    (root / "locked").chmod(0)
    try:
        names = [p["name"] for p in ops.projects_list({})["projects"]]
    finally:
        (root / "locked").chmod(0o755)
    assert "blog" in names


def test_the_hub_refuses_oversized_bodies_and_files_measured_in_bytes(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    base = f"/api/v1/nodes/{node}/projects/blog/files/compose.yaml"
    big = client.put(base, headers=headers, content=b"x" * (2 * 1024 * 1024))
    assert big.status_code == 413 and big.json()["error"]["code"] == "common.too_large"
    # 80,000 two-byte characters are under the character limit but over the byte limit.
    heavy = client.put(
        base, headers=headers, json={"content": "é" * 80_000, "expected_sha256": None}
    )
    assert heavy.status_code == 422
    stop.set()


# --- creating a project --------------------------------------------------------------------


def test_a_project_is_created_with_a_private_env_and_never_over_an_existing_one(root):
    done = ops.project_create({"project": "shop", "compose": COMPOSE, "env": "KEY=value\n"})
    assert done == {"project": "shop", "file": "compose.yaml", "env": True}
    folder = root / "shop"
    assert (folder / "compose.yaml").read_text() == COMPOSE
    assert stat.S_IMODE((folder / ".env").stat().st_mode) == 0o600
    assert code_of(ops.project_create, {"project": "shop", "compose": "x"}) == "worker.conflict"
    assert code_of(ops.project_create, {"project": "blog", "compose": "x"}) == "worker.conflict"
    assert (root / "blog" / ".env").read_text() == "SECRET=hunter2\n"
    ops.project_create({"project": "bare", "compose": COMPOSE})
    assert not (root / "bare" / ".env").exists()


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "x" * 65, 3, None])
def test_a_project_name_cannot_escape_the_folder(root, name):
    assert code_of(ops.project_create, {"project": name, "compose": "x"}) == "worker.invalid_path"
    assert sorted(p.name for p in root.parent.iterdir()) == ["stacks"]


def test_bad_or_oversized_content_leaves_nothing_behind(root):
    for bad in ({"compose": "a\x00b"}, {"compose": 5}, {"compose": "x", "env": "a\x00"}):
        assert code_of(ops.project_create, {"project": "new", **bad}) == "worker.invalid_path"
    big = "x" * (ops.MAX_FILE_BYTES + 1)
    assert code_of(ops.project_create, {"project": "new", "compose": big}) == "worker.too_large"
    huge_env = {"project": "new", "compose": "x", "env": "k" * (ops.MAX_ENV_BYTES + 1)}
    assert code_of(ops.project_create, huge_env) == "worker.too_large"
    assert not (root / "new").exists()


def test_hub_creates_a_project_with_the_right_and_audits_without_content(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    base = f"/api/v1/nodes/{node}/projects"
    made = client.post(
        base, headers=headers, json={"name": "shop", "compose": COMPOSE, "env": "TOKEN=zz\n"}
    )
    assert made.status_code == 201 and made.json()["env"] is True
    assert any(p["name"] == "shop" for p in client.get(base, headers=headers).json()["projects"])
    again = client.post(base, headers=headers, json={"name": "shop", "compose": COMPOSE})
    assert again.status_code == 409
    assert (
        client.post(base, headers=headers, json={"name": "../x", "compose": "a"}).status_code == 422
    )
    import sqlite3

    rows = (
        sqlite3.connect(tmp_path / "armadok.db")
        .execute("SELECT details FROM audit_log WHERE event = 'project.create'")
        .fetchall()
    )
    assert len(rows) == 2 and "TOKEN" not in str(rows) and "nginx" not in str(rows)
    uid = client.post(
        "/api/v1/users", headers=headers, json={"username": "bob", "password": PASSWORD}
    ).json()["id"]
    token = client.post(
        "/api/v1/auth/login", json={"username": "bob", "password": PASSWORD}
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    for right in ("nodes.read",):
        client.post(
            f"/api/v1/users/{uid}/grants",
            headers=headers,
            json={"right": right, "scope": f"node:{node}"},
        )
    assert client.post(base, headers=bob, json={"name": "x2", "compose": "a"}).status_code == 403
    stop.set()


def test_a_failed_creation_leaves_no_folder_and_env_temps_are_swept(root, monkeypatch):
    real = ops.safefs.write_regular
    calls = []

    def failing(folder, name, *args, **kwargs):
        calls.append(name)
        if name == "compose.yaml":
            raise OperationError("worker.forbidden")
        return real(folder, name, *args, **kwargs)

    monkeypatch.setattr(ops.safefs, "write_regular", failing)
    assert (
        code_of(ops.project_create, {"project": "half", "compose": "x", "env": "A=1"})
        == "worker.forbidden"
    )
    assert calls == [".env", "compose.yaml"]  # variables first, the Compose file last
    assert not (root / "half").exists()
    assert ops.TEMP_FILE.fullmatch("..env.armadok-0123456789abcdef.tmp")
    assert ops.TEMP_FILE.fullmatch(".compose.yaml.armadok-0123456789abcdef.tmp")


def test_importing_ignoring_and_unmanaging_change_only_what_armadok_shows(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    base = f"/api/v1/nodes/{node}/projects"
    found = client.get(f"{base}/discovered", headers=headers).json()["projects"]
    assert [(p["name"], p["state"]) for p in found] == [("blog", "discovered")]
    assert found[0]["services"] == ["web"] and found[0]["has_env"] is True
    # Nothing can be read or run on a project that was not imported: it is simply not there.
    assert client.get(f"{base}/blog/files/compose.yaml", headers=headers).status_code == 404
    assert (
        client.post(f"{base}/blog/actions", headers=headers, json={"action": "up"}).status_code
        == 404
    )
    assert (
        client.post(f"{base}/import", headers=headers, json={"projects": ["nope"]}).status_code
        == 404
    )
    assert (
        client.post(f"{base}/ignore", headers=headers, json={"projects": ["blog"]}).status_code
        == 200
    )
    assert (
        client.get(f"{base}/discovered", headers=headers).json()["projects"][0]["state"]
        == "ignored"
    )
    done = client.post(
        f"{base}/import",
        headers=headers,
        json={"projects": ["blog"], "labels": [" web ", "web", "a,b"]},
    )
    assert done.status_code == 200
    listed = client.get(base, headers=headers).json()["projects"]
    assert listed[0]["name"] == "blog" and listed[0]["labels"] == ["web", "a b"]
    # A managed project cannot be ignored; stopping to manage it brings it back as discovered.
    assert (
        client.post(f"{base}/ignore", headers=headers, json={"projects": ["blog"]}).status_code
        == 409
    )
    assert client.post(f"{base}/blog/unmanage", headers=headers).status_code == 204
    assert client.get(base, headers=headers).json()["projects"] == []
    assert (
        client.get(f"{base}/discovered", headers=headers).json()["projects"][0]["state"]
        == "discovered"
    )
    assert (root / "blog" / "compose.yaml").is_file()  # the disk was never touched
    import sqlite3

    events = [
        r[0]
        for r in sqlite3.connect(tmp_path / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'project.%' ORDER BY id"
        )
    ]
    client.post(
        f"/api/v1/nodes/{node}/projects/import", headers=headers, json={"projects": ["blog"]}
    )
    assert events == [
        "project.import",
        "project.ignore",
        "project.import",
        "project.ignore",
        "project.unmanage",
    ]
    stop.set()


def test_a_deeply_nested_compose_file_does_not_break_the_listing(root):
    (root / "blog" / "compose.yaml").write_text("[" * 20000)
    out = ops.projects_list({})
    assert out["projects"][0]["name"] == "blog" and out["projects"][0]["error"] == "invalid_yaml"


def test_a_big_compose_file_is_listed_without_being_read(root):
    big = "services:\n  web:\n    image: x\n" + "# padding\n" * 5000
    (root / "blog" / "compose.yaml").write_text(big)
    blog = ops.projects_list({})["projects"][0]
    assert blog["services"] == [] and blog["error"] is None


def test_a_hook_cannot_be_made_for_a_project_that_is_not_imported(live, root):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    hook = {
        "name": "ci",
        "action": "project.redeploy",
        "target": {"node_id": node, "project": "blog"},
    }
    assert client.post("/api/v1/hooks", headers=headers, json=hook).status_code == 404
    client.post(
        f"/api/v1/nodes/{node}/projects/import", headers=headers, json={"projects": ["blog"]}
    )
    assert client.post("/api/v1/hooks", headers=headers, json=hook).status_code == 201
    stop.set()
