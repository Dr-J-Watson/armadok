"""Node settings: places narrowed inside the environment's roots, sent with every call."""

import json
import logging
import re

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.worker import nodeconfig, ops, terminal_ops
from armadok.worker.operation import OperationError
from tests.test_members_admin import PASSWORD, login, owner_with_node, person


@pytest.fixture
def roots(tmp_path, monkeypatch):
    stacks = tmp_path / "stacks"
    for name in ("prod", "test", "prod/deep", "test/inner"):
        (stacks / name).mkdir(parents=True)
    (stacks / "afile").write_text("x")
    (stacks / "prod" / "deep" / "docker-compose.yml").write_text("services: {}\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(stacks))
    monkeypatch.setenv("ARMADOK_TRAEFIK_ACCESSLOG", str(logs / "access.log"))
    for var in ("ARMADOK_BACKUP_DIR", "ARMADOK_TRAEFIK_DIR", "ARMADOK_ACME_FILE"):
        monkeypatch.delenv(var, raising=False)
    return stacks, logs


def run(handler, **config):
    """Run `handler` the way a worker does, with the node's settings among the arguments."""
    return nodeconfig.scoped(handler)({"_config": config} if config else {})


def effective(name):
    return lambda args: {"path": nodeconfig.effective(name), "args": sorted(args)}


# --- the worker ---------------------------------------------------------


def test_without_settings_the_environment_root_is_used_unchanged(roots):
    stacks, _ = roots
    assert run(effective("projects_dir"))["path"] == str(stacks)


def test_a_folder_setting_narrows_to_a_subfolder_of_the_root(roots):
    stacks, _ = roots
    assert run(effective("projects_dir"), projects_dir="prod")["path"] == str(stacks / "prod")
    assert run(effective("projects_dir"), projects_dir="test/inner")["path"] == str(
        stacks / "test" / "inner"
    )


def test_the_settings_are_not_seen_by_the_operation_and_do_not_outlive_it(roots):
    stacks, _ = roots
    assert run(effective("projects_dir"), projects_dir="prod")["args"] == []
    assert nodeconfig.effective("projects_dir") == str(stacks)  # back to the root afterwards


@pytest.mark.parametrize(
    "value",
    ["/etc", "../etc", "prod/../../etc", "prod//..", ".hidden/..", "nope", "afile", "a b", "x" * 80,
     "a/b/c/d/e/f/g/h", "prod/deep"],  # the last one is a project folder
)  # fmt: skip
def test_a_value_that_leads_out_or_is_not_a_folder_is_refused(roots, value):
    with pytest.raises(OperationError) as refused:
        run(effective("projects_dir"), projects_dir=value)
    assert refused.value.code == "worker.config_invalid"


def test_a_link_to_the_outside_cannot_be_followed(roots, tmp_path):
    stacks, _ = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (stacks / "escape").symlink_to(outside)
    with pytest.raises(OperationError):
        run(effective("projects_dir"), projects_dir="escape")


def test_a_file_setting_only_renames_the_file_in_its_own_folder(roots):
    _, logs = roots
    assert run(effective("accesslog_file"), accesslog_file="other.log")["path"] == str(
        logs / "other.log"
    )
    for bad in ("../x.log", "sub/x.log", "/etc/passwd", "other.txt", "access"):
        with pytest.raises(OperationError):
            run(effective("accesslog_file"), accesslog_file=bad)


def test_a_setting_without_a_root_from_the_environment_stays_unset(roots):
    assert run(effective("backup_dir"), backup_dir="x")["path"] is None


def test_unknown_names_and_odd_values_in_the_settings_are_dropped(roots):
    stacks, _ = roots
    got = run(effective("projects_dir"), projects_dir="prod", evil="/etc")
    assert got["path"] == str(stacks / "prod")
    assert nodeconfig.scoped(effective("projects_dir"))({"_config": "junk"})["path"] == str(stacks)
    assert nodeconfig.scoped(effective("projects_dir"))({"_config": {"projects_dir": 5}})[
        "path"
    ] == str(stacks)


def test_check_says_why_a_value_would_be_refused(roots):
    got = nodeconfig.check(
        {"values": {"projects_dir": "nope", "backup_dir": "x", "acme_file": "", "evil": "x"}}
    )
    assert got["errors"] == {
        "projects_dir": "not_found",
        "backup_dir": "not_configured",
        "evil": "invalid",
    }
    assert nodeconfig.check({"values": {"projects_dir": "prod", "accesslog_file": "a.log"}}) == {
        "errors": {}
    }


def test_describe_shows_the_roots_and_what_is_in_force(roots):
    stacks, _ = roots
    got = nodeconfig.scoped(nodeconfig.describe)({"_config": {"projects_dir": "prod"}})
    place = got["settings"]["projects_dir"]
    assert place == {"kind": "dir", "root": str(stacks), "value": "prod", "valid": True}
    assert got["settings"]["backup_dir"]["root"] is None


def test_every_operation_of_the_worker_runs_scoped_and_the_list_has_the_config_ops(roots):
    stacks, _ = roots
    assert {"config.describe", "config.check"} <= set(ops.OPERATIONS)
    listed = ops.OPERATIONS["projects.list"]({"_config": {"projects_dir": "prod"}})
    assert [p["name"] for p in listed["projects"]] == ["deep"]  # only what is inside "prod"


def test_the_terminal_mounts_the_matching_subfolder_of_the_host_path(roots, monkeypatch):
    monkeypatch.setenv("ARMADOK_PROJECTS_HOST_DIR", "/srv/stacks")
    assert terminal_ops._host_root() == "/srv/stacks"
    seen = nodeconfig.scoped(lambda args: {"h": terminal_ops._host_root()})
    assert seen({"_config": {"projects_dir": "prod"}})["h"] == "/srv/stacks/prod"


# --- the hub ------------------------------------------------------------


@pytest.fixture
def box(tmp_path, caplog, roots):
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
        yield client, login(client, "admin"), app


def local_node(client, headers):
    return next(n["id"] for n in client.get("/api/v1/nodes", headers=headers).json() if n["local"])


def test_a_place_is_tested_kept_and_then_applies_to_every_call_of_the_node(box):
    client, admin, app = box
    node = local_node(client, admin)
    url = f"/api/v1/nodes/{node}/settings"
    first = client.get(url, headers=admin).json()
    assert first["places"]["projects_dir"]["value"] == "" and first["can_write"] is True
    assert first["places"]["projects_dir"]["root"]
    # A folder that does not exist is refused, and nothing is kept.
    refused = client.put(url, headers=admin, json={"places": {"projects_dir": "nope"}})
    assert (refused.status_code, refused.json()["error"]["code"]) == (
        422,
        "nodeconfig.invalid_place",
    )
    assert client.get(url, headers=admin).json()["places"]["projects_dir"]["value"] == ""
    assert (
        client.put(url, headers=admin, json={"places": {"projects_dir": "prod"}}).status_code == 200
    )
    after = client.get(url, headers=admin).json()["places"]["projects_dir"]
    assert after["value"] == "prod" and after["valid"] is True
    # The real projects listing now only sees what is inside the sub-folder.
    listed = client.get(f"/api/v1/nodes/{node}/projects", headers=admin).json()
    assert [p["name"] for p in listed["projects"]] == ["deep"]
    # Putting it back to the default.
    client.put(url, headers=admin, json={"places": {"projects_dir": ""}})
    assert client.get(url, headers=admin).json()["places"]["projects_dir"]["value"] == ""


def test_defaults_for_traefik_are_kept_without_asking_the_worker(box):
    client, admin, app = box
    node = local_node(client, admin)
    url = f"/api/v1/nodes/{node}/settings"
    done = client.put(
        url,
        headers=admin,
        json={"traefik_entrypoints": ["websecure"], "traefik_resolver": "lehttp"},
    )
    assert done.status_code == 200
    got = client.get(url, headers=admin).json()
    assert got["traefik_entrypoints"] == ["websecure"] and got["traefik_resolver"] == "lehttp"
    for bad in (
        {"traefik_entrypoints": ["a b"]},
        {"traefik_resolver": "x y"},
        {"places": {"evil": "x"}},
    ):
        assert client.put(url, headers=admin, json=bad).status_code == 422


def test_rights_machines_unseen_and_keys(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    url = f"/api/v1/nodes/{node}/settings"
    _, stranger = person(client, admin, "sam")
    assert client.get(url, headers=stranger).status_code == 404
    assert client.put(url, headers=stranger, json={}).status_code == 404
    # A reader of a node cannot change its settings.
    made = client.post(
        "/api/v1/roles",
        headers=admin,
        json={"name": "reader", "rights": ["nodes.read", "nodeconfig.read"]},
    )
    uid, reader = person(client, admin, "rea")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": uid, "role_id": made.json()["id"]},
    )
    assert client.get(url, headers=reader).json()["can_write"] is False
    assert client.put(url, headers=reader, json={"traefik_resolver": "x"}).status_code == 403
    # The owner can; a key cannot, even one that holds the right (it is sensitive).
    assert client.put(url, headers=olga, json={"traefik_resolver": "x"}).status_code == 200
    key = client.post(
        "/api/v1/apikeys", headers=olga, json={"name": "k", "rights": ["nodeconfig.write"]}
    )
    assert key.status_code >= 400


def test_a_place_is_refused_on_a_node_type_that_has_none_of_it(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)  # a Docker node
    url = f"/api/v1/nodes/{node}/settings"
    got = client.put(url, headers=olga, json={"places": {"traefik_dir": "x"}})
    assert (got.status_code, got.json()["error"]["code"]) == (422, "nodeconfig.not_for_node_type")
    assert set(client.get(url, headers=olga).json()["places"]) == {"projects_dir", "backup_dir"}


def test_changes_are_audited_with_the_names_and_never_fail_silently(box):
    client, admin, app = box
    node = local_node(client, admin)
    url = f"/api/v1/nodes/{node}/settings"
    client.put(url, headers=admin, json={"places": {"projects_dir": "prod"}})
    client.put(url, headers=admin, json={"places": {"projects_dir": "nope"}})
    text = json.dumps(client.get("/api/v1/audit", headers=admin).json())
    assert "nodeconfig.update" in text and "projects_dir" in text
    assert '"outcome": "failure"' in text or '"outcome":"failure"' in text


def test_the_registry_hands_the_places_of_a_node_to_every_call(box):
    client, admin, app = box
    node = local_node(client, admin)
    client.put(
        f"/api/v1/nodes/{node}/settings", headers=admin, json={"places": {"projects_dir": "test"}}
    )
    conn = app.state.settings.open_db()
    try:
        assert app.state.registry.worker_context(conn, node) == {"projects_dir": "test"}
    finally:
        conn.close()


def test_a_setting_a_caller_slips_into_the_arguments_never_reaches_the_worker(box):
    import asyncio

    from armadok.modules.workers.routes import call_operation

    client, admin, app = box
    node = local_node(client, admin)
    got = asyncio.run(
        call_operation(
            app, app.state.registry, node, "config.describe", {"_config": {"projects_dir": "test"}}
        )
    )
    assert got["settings"]["projects_dir"]["value"] == ""  # only what is stored counts


def test_a_reader_sees_only_the_last_name_of_each_root(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    made = client.post(
        "/api/v1/roles",
        headers=admin,
        json={"name": "reader2", "rights": ["nodes.read", "nodeconfig.read"]},
    )
    uid, reader = person(client, admin, "rea2")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": uid, "role_id": made.json()["id"]},
    )
    local = local_node(client, admin)
    shown = client.get(f"/api/v1/nodes/{local}/settings", headers=admin).json()
    assert shown["places"]["projects_dir"]["root"].startswith("/")  # a writer sees the real root
    got = client.get(f"/api/v1/nodes/{node}/settings", headers=reader).json()
    assert got["can_write"] is False
    assert all(not (p["root"] or "").startswith("/") for p in got["places"].values())


def test_places_of_a_type_the_node_no_longer_has_are_not_sent(box):
    from armadok.modules.nodeconfig import repository

    client, admin, app = box
    _, _, node = owner_with_node(client, admin)  # a Docker node
    conn = app.state.settings.open_db()
    try:
        repository.save(conn, node, {"traefik_dir": "x", "projects_dir": "y"}, "t")
        conn.commit()
        assert app.state.registry.worker_context(conn, node) == {"projects_dir": "y"}
    finally:
        conn.close()


def test_a_link_inside_the_root_is_refused_even_when_it_stays_inside(roots):
    stacks, _ = roots
    (stacks / "alias").symlink_to(stacks / "prod")
    with pytest.raises(OperationError):
        run(effective("projects_dir"), projects_dir="alias")
    assert nodeconfig.check({"values": {"projects_dir": "alias"}})["errors"] == {
        "projects_dir": "outside"
    }


def test_a_project_folder_and_the_hubs_own_data_cannot_be_narrowed_to(roots):
    stacks, _ = roots
    assert nodeconfig.check({"values": {"projects_dir": "prod/deep"}})["errors"] == {
        "projects_dir": "forbidden"
    }
    data = stacks / "test" / "inner"
    nodeconfig.protect(data)
    try:
        for value in ("test/inner", "test"):  # the folder itself, and one that contains it
            assert nodeconfig.check({"values": {"projects_dir": value}})["errors"] == {
                "projects_dir": "forbidden"
            }
        assert nodeconfig.check({"values": {"projects_dir": "prod"}}) == {"errors": {}}
    finally:
        nodeconfig._protected.remove(data.resolve())


def test_a_background_thread_keeps_the_settings_of_the_call_that_started_it(roots):
    import threading

    stacks, _ = roots
    seen, done = [], threading.Event()

    def work():
        seen.append(nodeconfig.effective("projects_dir"))
        done.set()

    nodeconfig.scoped(lambda args: nodeconfig.start_thread(work) or {})(
        {"_config": {"projects_dir": "prod"}}
    )
    assert done.wait(5)
    # Without this, a deployment would run in the root while the call saw the narrowed folder.
    assert seen == [str(stacks / "prod")]


def test_deployments_and_backups_start_their_thread_with_the_settings():
    from armadok.worker import backup_ops, compose_ops

    assert "nodeconfig.start_thread" in open(compose_ops.__file__).read()
    assert "nodeconfig.start_thread" in open(backup_ops.__file__).read()
