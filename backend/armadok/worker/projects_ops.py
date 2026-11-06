"""Operations on Compose projects stored on the node's disk.

The disk is the source of truth: this only reads and writes the Compose files of projects
that already live under ONE allowed folder (`ARMADOK_PROJECTS_DIR`). A project is a direct
sub-folder of it; a file is a Compose file of that folder, named like `compose.yaml` or
`docker-compose.prod.yml`. Names are checked against strict patterns, so there is no path to
traverse, and all disk access goes through `safefs` (no symlinks, no special files, no silent
overwrite).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from armadok.worker import nodeconfig, safefs
from armadok.worker.operation import Operation, OperationError

log = logging.getLogger("armadok.worker.projects")
PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
COMPOSE_FILE = re.compile(r"(?:docker-)?compose(?:\.[A-Za-z0-9_-]{1,30})?\.ya?ml")
TEMP_FILE = re.compile(r"\.+(?:(?:docker-)?compose.*|env)\.armadok-[0-9a-f]{16}\.tmp")
# JSON may spell one byte as up to six ("\u0000"), and a message is capped at 1 MiB: 128 KiB of
# content always fits, with room for the rest of the message.
MAX_FILE_BYTES = 128 * 1024
MAX_PROJECTS = 500
MAX_FILES_PER_PROJECT = 50
LIST_BUDGET_BYTES = 800 * 1024


def _root_path() -> Path:
    """Return the allowed folder (absolute, not `/`), or fail if none is configured."""
    configured = nodeconfig.effective("projects_dir")
    if not configured:
        raise OperationError("worker.projects_not_configured")
    root = Path(configured)
    if not root.is_absolute() or root == Path("/") or not root.is_dir():
        raise OperationError("worker.projects_not_configured")
    return root


class _Project:
    """An open project folder: use `.fd` for every operation inside it. Close when done."""

    def __init__(self, project: Any) -> None:
        if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
            raise OperationError("worker.invalid_path")
        root_fd = safefs.open_dir(str(_root_path()))
        try:
            self.fd = safefs.open_dir(project, root_fd)
        finally:
            os.close(root_fd)

    def __enter__(self) -> _Project:
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)


def _file_name(name: Any) -> str:
    if not isinstance(name, str) or not COMPOSE_FILE.fullmatch(name):
        raise OperationError("worker.invalid_path")
    return name


_ENV_FILE = re.compile(r"\.env")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MAX_SERVICES = 100
# Reading a Compose file to name its services is a courtesy, not a duty: a bigger file is listed
# without them, and a whole listing never spends more than this reading folders.
INSPECT_BYTES = 32 * 1024
INSPECT_SECONDS = 2.0


def _inspect(project_fd: int, names: list[str], read_files: bool = True) -> dict[str, Any]:
    """Say what a project folder holds, for the import screen: `.env`, `.git`, services.

    Only the main Compose file is read (the one Compose itself would pick first), within the
    usual size limit; a file that cannot be read or parsed is reported by a code, not fatal.
    """
    info: dict[str, Any] = {
        "has_env": any(True for _ in safefs.regular_names(project_fd, _ENV_FILE)),
        "has_git": False,
        "services": [],
        "error": None,
    }
    try:
        os.stat(".git", dir_fd=project_fd, follow_symlinks=False)
        info["has_git"] = True
    except OSError:
        pass
    if not read_files:
        return info
    order = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
    main = next((n for n in order if n in names), names[0])
    try:
        data, _ = safefs.read_regular(project_fd, main, MAX_FILE_BYTES)
        if len(data) > INSPECT_BYTES:
            return info  # too big to look into here; the project itself is still listed
        document = yaml.safe_load(data.decode("utf-8"))
    except OperationError:
        info["error"] = "too_large"
        return info
    except (yaml.YAMLError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        info["error"] = "invalid_yaml"
        return info
    services = document.get("services") if isinstance(document, dict) else None
    if isinstance(services, dict):
        info["services"] = [
            name for name in services if isinstance(name, str) and _SERVICE.fullmatch(name)
        ][:MAX_SERVICES]
    return info


def projects_list(args: dict[str, Any]) -> dict[str, Any]:
    """List the projects (sub-folders holding at least one Compose file) and their files.

    A folder that cannot be read is skipped, not fatal. The answer is kept within a byte
    budget; `truncated` says when projects were left out.
    """
    root_fd = safefs.open_dir(str(_root_path()))
    started = time.monotonic()
    projects: list[dict[str, Any]] = []
    truncated = False
    used = 0
    try:
        with os.scandir(root_fd) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if not PROJECT_NAME.fullmatch(entry.name):
                    continue
                try:
                    project_fd = safefs.open_dir(entry.name, root_fd)
                except OperationError:
                    continue  # a symlink, a plain file or an unreadable folder
                try:
                    names = sorted(safefs.regular_names(project_fd, COMPOSE_FILE))[
                        :MAX_FILES_PER_PROJECT
                    ]
                except OSError:
                    continue
                finally:
                    os.close(project_fd)
                if not names:
                    continue
                with_info = safefs.open_dir(entry.name, root_fd)
                try:
                    item = {
                        "name": entry.name,
                        "files": names,
                        **_inspect(with_info, names, time.monotonic() - started < INSPECT_SECONDS),
                    }
                finally:
                    os.close(with_info)
                used += len(json.dumps(item))
                if len(projects) >= MAX_PROJECTS or used > LIST_BUDGET_BYTES:
                    truncated = True
                    break
                projects.append(item)
    finally:
        os.close(root_fd)
    return {"projects": projects, "truncated": truncated}


def file_read(args: dict[str, Any]) -> dict[str, Any]:
    """Return a Compose file's text and its SHA-256 (the version a later write must name)."""
    name = _file_name(args.get("file"))
    with _Project(args.get("project")) as project:
        data, _ = safefs.read_regular(project.fd, name, MAX_FILE_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OperationError("worker.not_text") from None
    return {"content": text, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def file_write(args: dict[str, Any]) -> dict[str, Any]:
    """Replace a Compose file (or create it), refusing if it changed since it was read."""
    name = _file_name(args.get("file"))
    content, expected = args.get("content"), args.get("expected_sha256")
    if not isinstance(content, str) or (
        expected is not None
        and not (isinstance(expected, str) and safefs.SHA256.fullmatch(expected))
    ):
        raise OperationError("worker.invalid_path")
    with _Project(args.get("project")) as project:
        return safefs.write_regular(
            project.fd, name, content.encode("utf-8"), expected, MAX_FILE_BYTES, TEMP_FILE
        )


MAX_ENV_BYTES = 64 * 1024
ENV_FILE = ".env"
DEFAULT_COMPOSE = "compose.yaml"


def _text(value: Any, limit: int) -> bytes:
    """Return `value` as bytes if it is plain text within `limit` (no NUL), else refuse."""
    if not isinstance(value, str) or "\x00" in value:
        raise OperationError("worker.invalid_path")
    data = value.encode("utf-8")
    if len(data) > limit:
        raise OperationError("worker.too_large")
    return data


def _discard(root_fd: int, name: str, folder: int) -> None:
    """Remove a project folder that could not be finished (best effort, by name, no links)."""
    if folder >= 0:
        for entry in (ENV_FILE, DEFAULT_COMPOSE):
            try:
                os.unlink(entry, dir_fd=folder)
            except OSError:
                pass
        try:
            safefs.remove_stale_temps(folder, TEMP_FILE, max_age=0)
        except OSError:
            pass
        os.close(folder)
    try:
        os.rmdir(name, dir_fd=root_fd)
    except OSError:
        log.warning("could not remove the unfinished project folder")


def project_create(args: dict[str, Any]) -> dict[str, Any]:
    """Create a new project folder holding `compose.yaml` and, if given, a private `.env`.

    The folder must not exist yet and is made directly under the allowed folder. If anything
    fails halfway the new folder and its files are removed again, so a failure leaves nothing.
    """
    name = args.get("project")
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
        raise OperationError("worker.invalid_path")
    compose = _text(args.get("compose"), MAX_FILE_BYTES)
    env = _text(args["env"], MAX_ENV_BYTES) if args.get("env") else None
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        try:
            os.mkdir(name, 0o750, dir_fd=root_fd)
        except FileExistsError:
            raise OperationError("worker.conflict") from None
        except OSError:
            raise OperationError("worker.forbidden") from None
        folder = -1
        try:
            folder = safefs.open_dir(name, root_fd)
            # The `.env` goes first and the Compose file last: a folder only counts as a project
            # (and can only be deployed) once its Compose file exists, so nobody ever sees
            # a project that is waiting for its variables.
            if env is not None:
                safefs.write_regular(
                    folder, ENV_FILE, env, None, MAX_ENV_BYTES, TEMP_FILE, new_mode=0o600
                )
            safefs.write_regular(folder, DEFAULT_COMPOSE, compose, None, MAX_FILE_BYTES, TEMP_FILE)
        except BaseException:
            _discard(root_fd, name, folder)
            raise
        os.close(folder)
    finally:
        os.close(root_fd)
    return {"project": name, "file": DEFAULT_COMPOSE, "env": env is not None}


OPERATIONS: dict[str, Operation] = {
    "projects.create": project_create,
    "projects.list": projects_list,
    "projects.file.read": file_read,
    "projects.file.write": file_write,
}
