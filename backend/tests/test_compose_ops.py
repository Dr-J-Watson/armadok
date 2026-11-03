import json
import os
import stat
import time

import pytest

from armadok.worker import compose_ops as ops
from armadok.worker.operation import OperationError

FAKE = """#!/bin/sh
# A stand-in for `docker`. It cannot read settings from its environment (that is cleaned on
# purpose), so the test puts them in files next to it.
DIR=$(dirname "$0")
echo "$@" > "$DIR/rec/argv"
pwd -P > "$DIR/rec/cwd"
env > "$DIR/rec/env"
case "$(cat "$DIR/mode")" in
  ok) echo "done"; exit 0 ;;
  fail) echo "boom: something broke"; exit 3 ;;
  big) head -c 300000 /dev/zero | tr '\\0' 'x'; exit 0 ;;
  slow) sleep 30 ;;
  ps) cat "$DIR/ps.out" ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def stacks(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    (root / "blog").mkdir(parents=True)
    (root / "blog" / "compose.yaml").write_text("services: {}\n")
    record = tmp_path / "rec"
    record.mkdir()
    fake = tmp_path / "docker"
    (tmp_path / "mode").write_text("ok")
    service = {"Service": "web", "Name": "blog-web-1", "State": "running"}
    service |= {"Status": "Up 2 hours", "Image": "nginx:1"}
    (tmp_path / "ps.out").write_text(json.dumps(service) + "\nnot json\n")
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(root))
    monkeypatch.setenv("ARMADOK_DOCKER_BIN", str(fake))
    monkeypatch.setenv("ARMADOK_ENCRYPTION_KEY", "SECRET-OF-THE-HUB-THAT-MUST-NOT-LEAK")
    ops._tasks.clear()
    yield record
    ops._tasks.clear()


def set_mode(stacks, mode):
    (stacks.parent / "mode").write_text(mode)


def finish(task_id, wait=10):
    for _ in range(int(wait * 20)):
        state = ops.compose_task({"task": task_id})
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def code_of(call, args):
    with pytest.raises(OperationError) as caught:
        call(args)
    return caught.value.code


def test_up_builds_an_exact_argument_list_runs_in_the_project_and_reports(stacks, monkeypatch):
    set_mode(stacks, "ok")
    task = ops.compose_start(
        {
            "project": "blog",
            "action": "up",
            "build": True,
            "files": ["compose.yaml", "compose.prod.yaml"],
        }
    )["task"]
    result = finish(task)
    assert result["state"] == "success" and result["exit_code"] == 0 and "done" in result["output"]
    assert (stacks / "argv").read_text().split() == [
        "compose",
        "--project-name",
        "blog",
        "--file",
        "compose.yaml",
        "--file",
        "compose.prod.yaml",
        "up",
        "--detach",
        "--remove-orphans",
        "--build",
    ]
    assert (
        (stacks / "cwd").read_text().strip().endswith("/stacks/blog")
    )  # ran inside the project folder


def test_the_command_gets_a_clean_environment_with_no_secret_of_ours(stacks, monkeypatch):
    set_mode(stacks, "ok")
    finish(ops.compose_start({"project": "blog", "action": "stop"})["task"])
    seen = (stacks / "env").read_text()
    assert (
        "SECRET-OF-THE-HUB" not in seen
        and "ARMADOK_ENCRYPTION_KEY" not in seen
        and "RECORD_DIR" not in seen
    )
    assert "DOCKER_HOST=unix:///var/run/docker.sock" in seen and "PATH=/usr/local/bin" in seen


@pytest.mark.parametrize(
    "args",
    [
        {"project": "../x", "action": "up"},
        {"project": "blog;rm", "action": "up"},
        {"project": "blog", "action": "rm -rf"},
        {"project": "blog", "action": "up", "files": ["../../etc/passwd"]},
        {"project": "blog", "action": "up", "files": []},
        {"project": "blog", "action": "restart", "service": "web; id"},
        {"project": "blog", "action": "restart", "service": "--all"},
        {"project": "blog", "action": "up", "files": ["compose.yaml"] * 6},
        {"project": None, "action": "up"},
        {"project": "blog", "action": ["up"]},
    ],
)
def test_nothing_unvalidated_reaches_the_command_line(stacks, args):
    assert code_of(ops.compose_start, args) == "worker.invalid_path"
    assert not (stacks / "argv").exists()  # docker was never even started


def test_volumes_are_never_removed_and_services_can_be_targeted(stacks, monkeypatch):
    set_mode(stacks, "ok")
    finish(ops.compose_start({"project": "blog", "action": "down"})["task"])
    assert (
        "--volumes" not in (stacks / "argv").read_text()
        and "-v" not in (stacks / "argv").read_text().split()
    )
    finish(ops.compose_start({"project": "blog", "action": "restart", "service": "web"})["task"])
    assert (stacks / "argv").read_text().split()[-2:] == ["restart", "web"]


def test_failures_and_big_output_are_reported_and_bounded(stacks, monkeypatch):
    set_mode(stacks, "fail")
    failed = finish(ops.compose_start({"project": "blog", "action": "up"})["task"])
    assert failed["state"] == "failed" and failed["exit_code"] == 3 and "boom" in failed["output"]
    set_mode(stacks, "big")
    big = finish(ops.compose_start({"project": "blog", "action": "pull"})["task"])
    assert big["truncated"] is True and len(big["output"]) <= ops.MAX_OUTPUT_BYTES


def test_a_run_that_exceeds_its_deadline_is_killed_with_its_children(stacks, monkeypatch):
    set_mode(stacks, "slow")
    monkeypatch.setattr(ops, "ACTION_DEADLINE_SECONDS", 0.5)
    started = time.time()
    result = finish(ops.compose_start({"project": "blog", "action": "up"})["task"])
    assert (
        result["state"] == "failed"
        and result["output"] == "worker.timeout"
        and time.time() - started < 6
    )


def test_one_action_at_a_time_per_project_and_a_cap_on_running_tasks(stacks, monkeypatch, tmp_path):
    for name in ("a", "b", "c", "d", "e"):
        (tmp_path / "stacks" / name).mkdir()
        (tmp_path / "stacks" / name / "compose.yaml").write_text("services: {}\n")
    set_mode(stacks, "slow")
    monkeypatch.setattr(ops, "ACTION_DEADLINE_SECONDS", 30)
    first = ops.compose_start({"project": "blog", "action": "up"})["task"]
    assert (
        code_of(ops.compose_start, {"project": "blog", "action": "down"}) == "worker.project_busy"
    )
    for name in ("a", "b", "c"):
        ops.compose_start({"project": name, "action": "up"})
    assert (
        code_of(ops.compose_start, {"project": "d", "action": "up"}) == "worker.busy"
    )  # four at most
    assert ops.compose_task({"task": first})["state"] == "running"
    monkeypatch.setattr(ops, "ACTION_DEADLINE_SECONDS", 0.2)


def test_ps_logs_and_check_are_read_only_helpers(stacks, monkeypatch):
    set_mode(stacks, "ps")
    assert ops.compose_ps({"project": "blog"})["services"] == [
        {
            "service": "web",
            "name": "blog-web-1",
            "state": "running",
            "status": "Up 2 hours",
            "image": "nginx:1",
        }
    ]
    assert "--format json" in (stacks / "argv").read_text()
    set_mode(stacks, "ok")
    logs = ops.compose_logs({"project": "blog", "service": "web", "tail": 50})
    assert logs["output"].strip() == "done" and "--tail 50 web" in (stacks / "argv").read_text()
    for bad in (0, 5000, "5", True):
        assert code_of(ops.compose_logs, {"project": "blog", "tail": bad}) == "worker.invalid_path"
    assert ops.compose_check({"project": "blog"}) == {"valid": True, "errors": ""}
    set_mode(stacks, "fail")
    bad = ops.compose_check({"project": "blog"})
    assert bad["valid"] is False and "boom" in bad["errors"]


def test_a_project_folder_that_is_a_symlink_is_refused_before_anything_runs(
    stacks, monkeypatch, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "compose.yaml").write_text("services: {}\n")
    os.symlink(outside, tmp_path / "stacks" / "linked")
    set_mode(stacks, "ok")
    result = finish(ops.compose_start({"project": "linked", "action": "up"})["task"])
    assert result["state"] == "failed" and result["output"] == "worker.forbidden"
    assert not (stacks / "argv").exists()


def test_unknown_tasks_and_odd_ids_give_clean_errors(stacks):
    assert code_of(ops.compose_task, {"task": "0" * 16}) == "worker.not_found"
    assert code_of(ops.compose_task, {"task": "../x"}) == "worker.invalid_path"
    assert json  # (module imported for helpers above)
