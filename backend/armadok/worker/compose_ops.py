"""Deploying Compose projects: up, down, stop, start, restart, pull, build; status and logs.

This runs `docker compose` against a project folder, which is as powerful as it sounds: a
Compose file can mount host folders and run privileged containers. So the access to it is
controlled by a sensitive right on the hub, and here everything else is kept tight:

- no shell: the command is a list of arguments built only from validated names and a closed set
  of actions and flags;
- the project folder is opened with `safefs` (no symlinks) and the command runs inside that very
  folder handle, so the folder cannot be swapped for another between the check and the run;
- the environment given to the command is built from nothing: Armadok's own secrets (such as the
  encryption key, present in the hub's environment on the local node) are never inherited;
- output is captured with a size limit, runs have a deadline, and the whole process group is
  stopped when it is exceeded;
- long actions run in the background and return a task id at once, so the worker stays
  responsive (and the hub is not left waiting).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import stat
import subprocess
import threading
import time
from typing import Any

from armadok.worker.operation import Operation, OperationError
from armadok.worker.projects_ops import COMPOSE_FILE, PROJECT_NAME, _Project

SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
TASK_ID = re.compile(r"[0-9a-f]{16}")
ACTIONS = {
    "up": ["up", "--detach", "--remove-orphans"],
    "down": ["down", "--remove-orphans"],  # never `--volumes`: data is not removed from here
    "stop": ["stop"],
    "start": ["start"],
    "restart": ["restart"],
    "pull": ["pull"],
    "build": ["build"],
}
ACTION_DEADLINE_SECONDS = 15 * 60
QUICK_DEADLINE_SECONDS = 30
MAX_FILES = 5
MAX_OUTPUT_BYTES = 64 * 1024
MAX_LOG_BYTES = 128 * 1024
MAX_TAIL_LINES = 1000
MAX_TASKS_KEPT = 50
MAX_RUNNING_TASKS = 4

_lock = threading.Lock()
_tasks: dict[str, dict[str, Any]] = {}


def _docker() -> str:
    """Return the docker executable (overridable for tests)."""
    return os.environ.get("ARMADOK_DOCKER_BIN", "docker")


def clean_env() -> dict[str, str]:
    """Build the command's environment from nothing: only what `docker compose` needs.

    Nothing of Armadok's own environment is inherited (it may hold the encryption key).
    """
    socket_path = os.environ.get("ARMADOK_DOCKER_SOCKET", "/var/run/docker.sock")
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "DOCKER_CONFIG": f"/tmp/armadok-docker-config-{os.getuid()}",
        "DOCKER_HOST": f"unix://{socket_path}",
        "COMPOSE_ANSI": "never",
        "NO_COLOR": "1",
    }


def _files(value: Any) -> list[str]:
    """Validate the Compose files to use (names in the project folder), default `compose.yaml`."""
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_FILES:
        raise OperationError("worker.invalid_path")
    for name in value:
        if not isinstance(name, str) or not COMPOSE_FILE.fullmatch(name):
            raise OperationError("worker.invalid_path")
    return value


def _service(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SERVICE_NAME.fullmatch(value):
        raise OperationError("worker.invalid_path")
    return value


def _command(project: str, files: list[str], tail: list[str]) -> list[str]:
    """Build the argument list. `project` and `files` are already validated names."""
    argv = [_docker(), "compose", "--project-name", project]
    for name in files:
        argv += ["--file", name]
    return argv + tail


# Files Compose reads by itself from the project folder, besides the ones we name.
_IMPLICIT_FILES = (
    ".env",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)


def _refuse_links(folder_fd: int, extra: list[str]) -> None:
    """Refuse to run if a file Compose will read is not a plain file (e.g. a symlink).

    Compose follows links, so a link planted by a container that mounts the folder could make
    error messages quote files outside it (the hub's keys, for one).
    """
    for name in {*_IMPLICIT_FILES, *extra}:
        try:
            mode = os.stat(name, dir_fd=folder_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise OperationError("worker.invalid_path") from None
        if not stat.S_ISREG(mode):
            raise OperationError("worker.invalid_path")


def _run(
    argv: list[str], project: Any, deadline: float, cap: int, files: list[str] | None = None
) -> tuple[int, str, bool]:
    """Run a command inside the project folder; return `(exit code, output tail, truncated)`.

    The folder handle stays open for the whole run and the command's working directory is that
    handle, so it is the folder we checked, whatever happens to its name meanwhile.
    """
    with _Project(project) as folder:
        _refuse_links(folder.fd, files or [])
        proc = subprocess.Popen(  # noqa: S603  (argument list, no shell, validated names)
            argv,
            cwd=f"/proc/self/fd/{folder.fd}",
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own process group, so a timeout stops all of it
        )
        chunks: list[bytes] = []
        size = 0
        truncated = False

        def reader() -> None:
            nonlocal size, truncated
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(4096), b""):  # type: ignore[union-attr]
                if size < cap:
                    chunks.append(chunk[: cap - size])
                size += len(chunk)
                truncated = truncated or size > cap

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            code = proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            thread.join(timeout=2)
            if proc.stdout is not None:
                proc.stdout.close()
            raise OperationError("worker.timeout") from None
        thread.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
    return code, b"".join(chunks).decode("utf-8", "replace"), truncated


def _locate(args: dict[str, Any]) -> tuple[str, list[str]]:
    project = args.get("project")
    if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
        raise OperationError("worker.invalid_path")
    return project, _files(args.get("files"))


# --- long actions: started in the background, followed by task id -----------------------------


def _finish(task_id: str, code: int | None, output: str, truncated: bool) -> None:
    with _lock:
        task = _tasks[task_id]
        task.update(
            state="success" if code == 0 else "failed",
            exit_code=code,
            output=output,
            truncated=truncated,
            finished=time.time(),
        )


def _work(task_id: str, argv: list[str], project: str, files: list[str]) -> None:
    try:
        code, output, truncated = _run(
            argv, project, ACTION_DEADLINE_SECONDS, MAX_OUTPUT_BYTES, files
        )
        _finish(task_id, code, output, truncated)
    except OperationError as error:
        _finish(task_id, None, error.code, False)
    except Exception:  # noqa: BLE001  (details stay on the node)
        _finish(task_id, None, "worker.op_failed", False)


def compose_start(args: dict[str, Any]) -> dict[str, Any]:
    """Start `up`, `down`, `stop`, `start`, `restart`, `pull` or `build` and return a task id."""
    project, files = _locate(args)
    action = args.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise OperationError("worker.invalid_path")
    tail = list(ACTIONS[action])
    if action == "up":
        if args.get("build") is True:
            tail.append("--build")
        if args.get("pull") is True:
            tail += ["--pull", "always"]
    service = _service(args.get("service"))
    if service and action not in ("down",):
        tail.append(service)
    argv = _command(project, files, tail)
    with _lock:
        running = [t for t in _tasks.values() if t["state"] == "running"]
        if any(t["project"] == project for t in running):
            raise OperationError("worker.project_busy")
        if len(running) >= MAX_RUNNING_TASKS:
            raise OperationError("worker.busy")
        task_id = secrets.token_hex(8)
        _tasks[task_id] = {
            "project": project,
            "action": action,
            "state": "running",
            "exit_code": None,
            "output": "",
            "truncated": False,
            "started": time.time(),
            "finished": None,
        }
        for old in sorted(_tasks, key=lambda k: _tasks[k]["started"])[:-MAX_TASKS_KEPT]:
            if _tasks[old]["state"] != "running":
                del _tasks[old]
    threading.Thread(target=_work, args=(task_id, argv, project, files), daemon=True).start()
    return {"task": task_id}


def compose_task(args: dict[str, Any]) -> dict[str, Any]:
    """Return the state of a task started here, with the tail of its output."""
    task_id = args.get("task")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise OperationError("worker.invalid_path")
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            raise OperationError("worker.not_found")
        return {
            k: task[k] for k in ("project", "action", "state", "exit_code", "output", "truncated")
        }


# --- quick, synchronous reads -----------------------------------------------------------------


def compose_ps(args: dict[str, Any]) -> dict[str, Any]:
    """Return the state of a project's containers."""
    project, files = _locate(args)
    argv = _command(project, files, ["ps", "--all", "--format", "json"])
    code, output, _ = _run(argv, project, QUICK_DEADLINE_SECONDS, MAX_LOG_BYTES, files)
    if code != 0:
        raise OperationError("worker.compose_failed")
    services = []
    for line in output.splitlines():
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict):
            services.append(
                {
                    "service": str(raw.get("Service", ""))[:64],
                    "name": str(raw.get("Name", ""))[:128],
                    "state": str(raw.get("State", ""))[:30],
                    "status": str(raw.get("Status", ""))[:200],
                    "image": str(raw.get("Image", ""))[:300],
                }
            )
    return {"services": services[:200]}


def compose_logs(args: dict[str, Any]) -> dict[str, Any]:
    """Return the tail of a project's (or one service's) logs."""
    project, files = _locate(args)
    tail = args.get("tail", 200)
    if type(tail) is not int or not 1 <= tail <= MAX_TAIL_LINES:
        raise OperationError("worker.invalid_path")
    extra = ["logs", "--no-color", "--tail", str(tail)]
    service = _service(args.get("service"))
    if service:
        extra.append(service)
    code, output, truncated = _run(
        _command(project, files, extra), project, QUICK_DEADLINE_SECONDS, MAX_LOG_BYTES, files
    )
    if code != 0:
        raise OperationError("worker.compose_failed")
    return {"output": output, "truncated": truncated}


def compose_check(args: dict[str, Any]) -> dict[str, Any]:
    """Check that the project's Compose file is valid, without changing anything."""
    project, files = _locate(args)
    code, output, _ = _run(
        _command(project, files, ["config", "--quiet"]),
        project,
        QUICK_DEADLINE_SECONDS,
        4096,
        files,
    )
    return {"valid": code == 0, "errors": "" if code == 0 else output[:4096]}


OPERATIONS: dict[str, Operation] = {
    "compose.start": compose_start,
    "compose.task": compose_task,
    "compose.ps": compose_ps,
    "compose.logs": compose_logs,
    "compose.check": compose_check,
}
