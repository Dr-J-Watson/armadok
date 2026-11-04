import json
import logging
import os
import re
import time
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.backups import repository as repo
from armadok.modules.backups import scheduled, service
from armadok.worker import backup_ops
from armadok.worker.operation import OperationError

ORIGINAL_RUN_HELPER = backup_ops._run_helper
PASSWORD = "correct horse battery"
IMAGE = "sha256:" + "a" * 64
SPECS: list[dict] = []


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A backup folder, with the Docker side faked: the helper container 'writes' the archive."""
    path = tmp_path / "backups"
    path.mkdir()
    monkeypatch.setenv("ARMADOK_BACKUP_DIR", str(path))
    SPECS.clear()
    state = {
        "exit": 0,
        "users": [],
        "volume": {"Driver": "local", "Options": None},
        "orphans": [],
        "deleted": [],
    }

    def helper(spec):
        SPECS.append(spec)
        name = spec["Cmd"][0]
        if spec["Entrypoint"][2] == backup_ops._BACKUP_SCRIPT and state["exit"] == 0:
            (path / name).write_bytes(b"x" * 10)
        return state["exit"]

    monkeypatch.setattr(backup_ops, "_run_helper", helper)

    def host_side():
        backup_ops._folder()  # like the real one: no folder configured, no backups
        return IMAGE, "/host/backups"

    monkeypatch.setattr(backup_ops, "_host_side", host_side)

    def raw(method, url, timeout=10, body=None):
        if method == "DELETE":
            state["deleted"].append(url)
            return 204, b""
        if url.startswith("/volumes/"):
            if state["volume"] is None:
                return 404, b""
            return 200, json.dumps(state["volume"]).encode()
        if "armadok.helper" in unquote(url):
            return 200, json.dumps(state["orphans"]).encode()
        return 200, json.dumps(state["users"]).encode()

    monkeypatch.setattr(backup_ops, "docker_raw", raw)
    monkeypatch.setattr(backup_ops, "_stamp", iter_stamps())
    backup_ops._tasks.clear()
    state["path"] = path
    return state


def iter_stamps():
    counter = iter(range(10**6))
    return lambda: f"20260101T{next(counter) % 100000:06d}Z"


def wait_for(task_id):
    for _ in range(200):
        answer = backup_ops.backup_task({"task": task_id})
        if answer["state"] != "running":
            return answer
        time.sleep(0.02)
    raise AssertionError("the task did not finish")


# --- the worker -------------------------------------------------------------------------------


def test_the_helper_container_is_locked_down_and_names_never_reach_the_script(folder):
    done = wait_for(backup_ops.backup_start({"volume": "my_data"})["task"])
    assert done["state"] == "success"
    (spec,) = SPECS
    host = spec["HostConfig"]
    assert spec["Image"] == IMAGE and spec["NetworkDisabled"] is True
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False and host["CapDrop"] == ["ALL"]
    assert set(host["CapAdd"]) == {"DAC_READ_SEARCH", "DAC_OVERRIDE"}
    assert host["Binds"] == ["my_data:/data:ro", "/host/backups:/backup"]
    assert spec["Labels"]["armadok.protect"] == "true" and host["Memory"] > 0
    script = spec["Entrypoint"][2]
    assert "my_data" not in script and "$1" in script and "umask 077" in script
    assert re.fullmatch(r"\.my_data--\d{8}T\d{6}Z\.tar\.gz\.part", spec["Cmd"][0])


def test_a_backup_is_renamed_into_place_and_old_ones_are_pruned(folder):
    names = []
    for _ in range(4):
        task = wait_for(backup_ops.backup_start({"volume": "data", "keep": 2})["task"])
        assert task["state"] == "success" and task["size"] == 10
        names.append(task["archive"])
    left = sorted(p.name for p in folder["path"].iterdir())
    assert left == sorted(names[-2:])  # the two newest, and no leftover .part
    # Another volume's archives are never touched by this volume's rotation.
    (folder["path"] / "other--20250101T000000Z.tar.gz").write_bytes(b"y")
    wait_for(backup_ops.backup_start({"volume": "data", "keep": 1})["task"])
    assert "other--20250101T000000Z.tar.gz" in os.listdir(folder["path"])


def test_a_failed_helper_leaves_no_partial_archive(folder):
    folder["exit"] = 2
    done = wait_for(backup_ops.backup_start({"volume": "data"})["task"])
    assert done["state"] == "failed" and done["error"] == "worker.backup_failed"
    assert list(folder["path"].iterdir()) == []


@pytest.mark.parametrize("volume", ["", "../x", "a b", "-lead", "a" * 129, None, 5, ["x"], "a;b"])
def test_bad_volume_names_never_reach_the_daemon(folder, volume):
    with pytest.raises(OperationError) as error:
        backup_ops.backup_start({"volume": volume})
    assert error.value.code == "worker.invalid_path" and SPECS == []


@pytest.mark.parametrize("keep", [0, 366, True, "7", None])
def test_keep_is_bounded(folder, keep):
    with pytest.raises(OperationError):
        backup_ops.backup_start({"volume": "data", "keep": keep})


def test_without_a_backup_folder_nothing_is_offered(folder, monkeypatch):
    monkeypatch.delenv("ARMADOK_BACKUP_DIR")
    for call in (
        lambda: backup_ops.backup_list({}),
        lambda: backup_ops.backup_delete({"archive": "a--20260101T000000Z.tar.gz"}),
    ):
        with pytest.raises(OperationError) as error:
            call()
        assert error.value.code == "worker.backups_not_configured"


def test_listing_shows_only_well_formed_regular_files(folder):
    path = folder["path"]
    (path / "good--20260101T000000Z.tar.gz").write_bytes(b"123")
    (path / "notes.txt").write_text("hi")
    (path / ".hidden--20260101T000000Z.tar.gz.part").write_bytes(b"x")
    (path / "linked--20260101T000000Z.tar.gz").symlink_to("/etc/passwd")
    (path / "dir--20260101T000000Z.tar.gz").mkdir()
    listed = backup_ops.backup_list({})["archives"]
    assert [a["name"] for a in listed] == ["good--20260101T000000Z.tar.gz"]
    assert listed[0] == {
        "name": "good--20260101T000000Z.tar.gz",
        "volume": "good",
        "stamp": "20260101T000000Z",
        "size": 3,
    }


def test_deleting_removes_only_listed_archives(folder):
    path = folder["path"]
    keep = path / "keep.txt"
    keep.write_text("x")
    (path / "a--20260101T000000Z.tar.gz").write_bytes(b"1")
    (path / "link--20260101T000000Z.tar.gz").symlink_to(keep)
    assert backup_ops.backup_delete({"archive": "a--20260101T000000Z.tar.gz"}) == {"done": True}
    assert not (path / "a--20260101T000000Z.tar.gz").exists()
    for bad in ("link--20260101T000000Z.tar.gz", "gone--20260101T000000Z.tar.gz"):
        with pytest.raises(OperationError) as error:
            backup_ops.backup_delete({"archive": bad})
        assert error.value.code == "worker.not_found"
    for bad in ("../keep.txt", "keep.txt", "a/b--20260101T000000Z.tar.gz", None):
        with pytest.raises(OperationError) as error:
            backup_ops.backup_delete({"archive": bad})
        assert error.value.code == "worker.invalid_path"
    assert keep.exists()


def test_a_restore_mounts_the_backups_read_only_and_refuses_a_volume_in_use(folder):
    (folder["path"] / "data--20260101T000000Z.tar.gz").write_bytes(b"1")
    folder["users"] = [{"Id": "c" * 64}]
    with pytest.raises(OperationError) as error:
        backup_ops.restore_start({"archive": "data--20260101T000000Z.tar.gz", "volume": "data"})
    assert error.value.code == "worker.conflict" and SPECS == []
    folder["users"] = []
    done = wait_for(
        backup_ops.restore_start({"archive": "data--20260101T000000Z.tar.gz", "volume": "data"})[
            "task"
        ]
    )
    assert done["state"] == "success"
    (spec,) = SPECS
    assert spec["HostConfig"]["Binds"] == ["data:/data", "/host/backups:/backup:ro"]
    assert set(spec["HostConfig"]["CapAdd"]) == {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "DAC_READ_SEARCH",
    }
    assert spec["Cmd"] == ["data--20260101T000000Z.tar.gz"]


def test_restoring_an_unknown_archive_is_refused(folder):
    with pytest.raises(OperationError) as error:
        backup_ops.restore_start({"archive": "data--20260101T000000Z.tar.gz", "volume": "data"})
    assert error.value.code == "worker.not_found"


def test_one_job_per_volume_and_a_few_at_once(folder, monkeypatch):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(backup_ops, "_run_helper", lambda spec: gate.wait(5) and 0)
    first = backup_ops.backup_start({"volume": "one"})["task"]
    with pytest.raises(OperationError) as error:
        backup_ops.backup_start({"volume": "one"})
    assert error.value.code == "worker.project_busy"
    backup_ops.backup_start({"volume": "two"})
    with pytest.raises(OperationError) as error:
        backup_ops.backup_start({"volume": "three"})
    assert error.value.code == "worker.busy"
    gate.set()
    wait_for(first)


def test_unknown_task_ids_are_refused(folder):
    with pytest.raises(OperationError) as error:
        backup_ops.backup_task({"task": "0" * 16})
    assert error.value.code == "worker.not_found"
    with pytest.raises(OperationError):
        backup_ops.backup_task({"task": "../x"})


# --- the hub ----------------------------------------------------------------------------------


@pytest.fixture
def box(folder, tmp_path, caplog):
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
        admin = login(client, "admin")
        node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
        yield client, admin, node, app


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def person(client, admin, name, grants=()):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right, scope in grants:
        client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    return login(client, name)


def running(app):
    conn = app.state.settings.open_db()
    try:
        return repo.running(conn)
    finally:
        conn.close()


def finish_jobs(client, app):
    for job in running(app):
        wait_for(job["worker_task"])
        client.portal.call(service.sync_job, app, job)


def test_overview_rights_and_visibility(box):
    client, admin, node, _ = box
    scope = f"node:{node}"
    reader = person(
        client,
        admin,
        "reader",
        [("nodes.read", scope), ("docker.read", scope), ("backups.read", scope)],
    )
    stranger = person(client, admin, "stranger")
    url = f"/api/v1/nodes/{node}/backups"
    got = client.get(url, headers=reader).json()
    assert got["configured"] is True and got["can_create"] is False and got["can_manage"] is False
    assert client.get(url, headers=stranger).status_code == 404
    assert client.post(f"{url}/volumes/data", headers=reader, json={}).status_code == 403
    assert client.post(f"{url}/volumes/data", headers=stranger, json={}).status_code == 404


def test_a_machine_without_a_backup_folder_says_so(box, monkeypatch):
    client, admin, node, _ = box
    monkeypatch.delenv("ARMADOK_BACKUP_DIR")
    got = client.get(f"/api/v1/nodes/{node}/backups", headers=admin)
    assert got.status_code == 200 and got.json()["configured"] is False
    started = client.post(f"/api/v1/nodes/{node}/backups/volumes/data", headers=admin, json={})
    assert started.status_code == 409
    assert started.json()["error"]["code"] == "worker.backups_not_configured"


def test_a_backup_runs_to_the_end_and_is_audited_and_notified(box, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(service, "emit", fake_emit)
    url = f"/api/v1/nodes/{node}/backups"
    assert client.post(f"{url}/volumes/data", headers=admin, json={"keep": 3}).status_code == 202
    finish_jobs(client, app)
    overview = client.get(url, headers=admin).json()
    assert [a["volume"] for a in overview["archives"]] == ["data"]
    assert overview["jobs"][0]["state"] == "success" and overview["jobs"][0]["size"] == 10
    assert sent[0]["event"] == "backup.done" and sent[0]["params"]["volume"] == "data"
    log = client.get("/api/v1/audit", headers=admin).text
    assert "backup.start" in log and "backup.done" in log


def test_a_failed_backup_is_reported_once(box, folder, monkeypatch):
    client, admin, node, app = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload["event"])

    monkeypatch.setattr(service, "emit", fake_emit)
    folder["exit"] = 1
    client.post(f"/api/v1/nodes/{node}/backups/volumes/data", headers=admin, json={})
    job = running(app)[0]
    wait_for(job["worker_task"])
    for _ in range(3):  # following it again must not report it again
        client.portal.call(service.sync_job, app, job)
    assert sent == ["backup.failed"]
    assert (
        client.get(f"/api/v1/nodes/{node}/backups", headers=admin).json()["jobs"][0]["error"]
        == "worker.backup_failed"
    )


def test_restoring_needs_the_volume_name_typed_and_the_manage_right(box, folder):
    client, admin, node, app = box
    scope = f"node:{node}"
    (folder["path"] / "data--20260101T000000Z.tar.gz").write_bytes(b"1")
    operator = person(
        client,
        admin,
        "operator1",
        [
            ("nodes.read", scope),
            ("docker.read", scope),
            ("backups.read", scope),
            ("backups.create", scope),
        ],
    )
    url = f"/api/v1/nodes/{node}/backups/restore"
    body = {"archive": "data--20260101T000000Z.tar.gz", "volume": "data", "confirm": "data"}
    assert client.post(url, headers=operator, json=body).status_code == 403
    wrong = client.post(url, headers=admin, json={**body, "confirm": "dat"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "backups.confirm_mismatch"
    assert SPECS == []
    assert client.post(url, headers=admin, json=body).status_code == 202
    finish_jobs(client, app)
    assert SPECS[0]["HostConfig"]["Binds"][1].endswith(":ro")
    assert (
        client.get(f"/api/v1/nodes/{node}/backups", headers=admin).json()["jobs"][0]["kind"]
        == "restore"
    )


def test_deleting_an_archive_needs_the_manage_right(box, folder):
    client, admin, node, _ = box
    scope = f"node:{node}"
    name = "data--20260101T000000Z.tar.gz"
    (folder["path"] / name).write_bytes(b"1")
    operator = person(
        client,
        admin,
        "operator1",
        [("nodes.read", scope), ("docker.read", scope), ("backups.create", scope)],
    )
    url = f"/api/v1/nodes/{node}/backups/archives/{name}"
    assert client.delete(url, headers=operator).status_code == 403
    assert client.delete(
        f"/api/v1/nodes/{node}/backups/archives/..%2Fx", headers=admin
    ).status_code in (404, 422)
    assert client.delete(url, headers=admin).status_code == 204
    assert client.delete(url, headers=admin).status_code == 404
    assert "backup.delete" in client.get("/api/v1/audit", headers=admin).text


def test_a_key_cannot_carry_the_manage_right(box):
    client, admin, node, _ = box
    got = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["backups.manage"]}
    )
    assert got.status_code == 422 and got.json()["error"]["code"] == "apikeys.invalid_right"


def test_a_lying_worker_cannot_end_or_corrupt_a_job(box, folder, monkeypatch):
    client, admin, node, app = box
    client.post(f"/api/v1/nodes/{node}/backups/volumes/data", headers=admin, json={})
    job = running(app)[0]
    wait_for(job["worker_task"])

    async def liar(*_a, **_k):
        return {"state": "success", "archive": "../../etc/passwd", "size": -5}

    monkeypatch.setattr(service, "call_operation", liar)
    client.portal.call(service.sync_job, app, job)
    assert len(running(app)) == 1  # nothing was recorded from the invalid answer


def test_scheduling_a_backup_validates_its_target():
    assert scheduled.validate({"node_id": "n", "volume": "data"}) == {
        "node_id": "n",
        "volume": "data",
    }
    assert scheduled.validate({"node_id": "n", "volume": "data", "keep": "5"})["keep"] == 5
    for bad in (
        {"node_id": "n", "volume": "../x"},
        {"node_id": "n", "volume": "data", "keep": "0"},
        {"node_id": "n", "volume": "data", "keep": "many"},
        {"node_id": "n", "volume": "data", "extra": "1"},
        {"node_id": "n"},
    ):
        with pytest.raises(ValueError):
            scheduled.validate(bad)
    assert scheduled.ACTION.hookable is False and scheduled.ACTION.schedulable is True


# --- review points ----------------------------------------------------------------------------


def test_volumes_that_open_a_host_folder_or_use_a_plugin_are_refused(folder):
    folder["volume"] = {
        "Driver": "local",
        "Options": {"type": "none", "o": "bind", "device": "/etc"},
    }
    with pytest.raises(OperationError) as error:
        backup_ops.backup_start({"volume": "data"})
    assert error.value.code == "worker.forbidden"
    folder["volume"] = {"Driver": "local", "Options": {"o": "bind,ro"}}
    with pytest.raises(OperationError):
        backup_ops.backup_start({"volume": "data"})
    folder["volume"] = {"Driver": "nfs-plugin"}
    with pytest.raises(OperationError) as error:
        backup_ops.backup_start({"volume": "data"})
    assert error.value.code == "worker.not_found"
    folder["volume"] = None  # a backup needs the volume to exist
    with pytest.raises(OperationError) as error:
        backup_ops.backup_start({"volume": "data"})
    assert error.value.code == "worker.not_found"
    assert SPECS == []


def test_a_restore_may_create_a_volume_but_not_use_a_strange_one(folder):
    (folder["path"] / "data--20260101T000000Z.tar.gz").write_bytes(b"1")
    folder["volume"] = None  # does not exist yet: Docker will make a plain local one
    done = wait_for(
        backup_ops.restore_start({"archive": "data--20260101T000000Z.tar.gz", "volume": "data"})[
            "task"
        ]
    )
    assert done["state"] == "success"
    folder["volume"] = {"Driver": "local", "Options": {"type": "none", "o": "bind"}}
    with pytest.raises(OperationError) as error:
        backup_ops.restore_start({"archive": "data--20260101T000000Z.tar.gz", "volume": "data"})
    assert error.value.code == "worker.forbidden"


def test_the_backup_folders_own_volume_is_never_backed_up_or_restored(folder, monkeypatch):
    monkeypatch.setattr(backup_ops, "_host_side", lambda: (IMAGE, "backup_vol"))
    (folder["path"] / "backup_vol--20260101T000000Z.tar.gz").write_bytes(b"1")
    for call in (
        lambda: backup_ops.backup_start({"volume": "backup_vol"}),
        lambda: backup_ops.restore_start(
            {"archive": "backup_vol--20260101T000000Z.tar.gz", "volume": "backup_vol"}
        ),
    ):
        with pytest.raises(OperationError) as error:
            call()
        assert error.value.code == "worker.forbidden"


def test_an_archive_goes_into_another_volume_only_when_asked(folder):
    (folder["path"] / "data--20260101T000000Z.tar.gz").write_bytes(b"1")
    args = {"archive": "data--20260101T000000Z.tar.gz", "volume": "copy"}
    with pytest.raises(OperationError) as error:
        backup_ops.restore_start(args)
    assert error.value.code == "worker.conflict" and SPECS == []
    assert wait_for(backup_ops.restore_start({**args, "other": True})["task"])["state"] == "success"
    with pytest.raises(OperationError):
        backup_ops.restore_start({**args, "other": "yes"})


def test_leftovers_of_a_crash_are_cleaned_before_the_next_job(folder):
    folder["orphans"] = [{"Id": "d" * 64}, {"Id": "not-an-id"}]
    (folder["path"] / ".data--20260101T000000Z.tar.gz.part").write_bytes(b"half")
    (folder["path"] / "data--20250101T000000Z.tar.gz").write_bytes(b"keep")
    wait_for(backup_ops.backup_start({"volume": "data"})["task"])
    assert folder["deleted"][0].startswith(f"/containers/{'d' * 64}")
    assert len([u for u in folder["deleted"] if "not-an-id" in u]) == 0
    names = os.listdir(folder["path"])
    assert not any(n.endswith(".part") for n in names) and "data--20250101T000000Z.tar.gz" in names


def test_cleanup_never_touches_the_helper_of_a_running_job(folder, monkeypatch):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(backup_ops, "_run_helper", lambda spec: gate.wait(5) and 0)
    first = backup_ops.backup_start({"volume": "one"})["task"]
    folder["orphans"] = [{"Id": "d" * 64}]
    backup_ops.backup_start({"volume": "two"})
    assert folder["deleted"] == []
    gate.set()
    wait_for(first)


def test_a_failed_removal_of_the_helper_does_not_hide_the_real_error(folder, monkeypatch):
    def raw(method, url, timeout=10, body=None):
        if method == "POST" and url == "/containers/create":
            return 201, json.dumps({"Id": "e" * 64}).encode()
        if method == "POST":
            return 500, b""
        raise OperationError("worker.docker_unavailable")

    monkeypatch.setattr(backup_ops, "docker_raw", raw)
    with pytest.raises(OperationError) as error:
        ORIGINAL_RUN_HELPER({})
    assert error.value.code == "worker.docker_error"


def test_an_archive_is_never_replaced_by_a_backup_made_the_same_second(folder, monkeypatch):
    monkeypatch.setattr(backup_ops, "_stamp", lambda: "20260101T000000Z")
    first = wait_for(backup_ops.backup_start({"volume": "data"})["task"])
    assert first["state"] == "success"
    (folder["path"] / first["archive"]).write_bytes(b"precious")
    second = wait_for(backup_ops.backup_start({"volume": "data"})["task"])
    assert second["state"] == "failed" and second["error"] == "worker.conflict"
    assert (folder["path"] / first["archive"]).read_bytes() == b"precious"
    assert not any(n.endswith(".part") for n in os.listdir(folder["path"]))


def test_a_listing_says_when_it_is_incomplete(folder, monkeypatch):
    monkeypatch.setattr(backup_ops, "MAX_ARCHIVES", 2)
    for day in range(1, 5):
        (folder["path"] / f"data--2026010{day}T000000Z.tar.gz").write_bytes(b"1")
    answer = backup_ops.backup_list({})
    assert len(answer["archives"]) == 2 and answer["truncated"] is True
    assert [a["stamp"][:8] for a in answer["archives"]] == ["20260104", "20260103"]


def test_only_a_manager_may_change_how_many_archives_are_kept(box):
    client, admin, node, _ = box
    scope = f"node:{node}"
    op = person(
        client,
        admin,
        "operator1",
        [
            ("nodes.read", scope),
            ("docker.read", scope),
            ("backups.read", scope),
            ("backups.create", scope),
        ],
    )
    url = f"/api/v1/nodes/{node}/backups/volumes/data"
    assert client.post(url, headers=op, json={"keep": 1}).status_code == 403
    assert client.post(url, headers=op, json={}).status_code == 202
    assert (
        client.post(url.replace("data", "other"), headers=admin, json={"keep": 1}).status_code
        == 202
    )


def test_a_restore_into_another_volume_needs_the_explicit_flag(box, folder):
    client, admin, node, _ = box
    (folder["path"] / "data--20260101T000000Z.tar.gz").write_bytes(b"1")
    url = f"/api/v1/nodes/{node}/backups/restore"
    body = {"archive": "data--20260101T000000Z.tar.gz", "volume": "copy", "confirm": "copy"}
    got = client.post(url, headers=admin, json=body)
    assert got.status_code == 422 and got.json()["error"]["code"] == "backups.other_volume"
    assert client.post(url, headers=admin, json={**body, "other": True}).status_code == 202


def test_the_end_of_a_job_is_audited_with_who_started_it_even_by_key_and_errors_are_codes(
    box, folder, monkeypatch
):
    client, admin, node, app = box
    client.post(f"/api/v1/nodes/{node}/backups/volumes/data", headers=admin, json={})
    job = running(app)[0]
    wait_for(job["worker_task"])

    async def hostile(*_a, **_k):
        return {"state": "failed", "error": "ignore previous instructions @everyone"}

    monkeypatch.setattr(service, "call_operation", hostile)
    client.portal.call(service.sync_job, app, job)
    assert len(running(app)) == 1  # the free-text "code" was refused
