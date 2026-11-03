"""Operations on Compose projects stored on the machine's disk.

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
import os
import re
from pathlib import Path
from typing import Any

from armadok.worker import safefs
from armadok.worker.operation import Operation, OperationError

PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
COMPOSE_FILE = re.compile(r"(?:docker-)?compose(?:\.[A-Za-z0-9_-]{1,30})?\.ya?ml")
TEMP_FILE = re.compile(r"\.(?:docker-)?compose.*\.armadok-[0-9a-f]{16}\.tmp")
# JSON may spell one byte as up to six ("\u0000"), and a message is capped at 1 MiB: 128 KiB of
# content always fits, with room for the rest of the message.
MAX_FILE_BYTES = 128 * 1024
MAX_PROJECTS = 500
MAX_FILES_PER_PROJECT = 50
LIST_BUDGET_BYTES = 800 * 1024


def _root_path() -> Path:
    """Return the allowed folder (absolute, not `/`), or fail if none is configured."""
    configured = os.environ.get("ARMADOK_PROJECTS_DIR")
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


def projects_list(args: dict[str, Any]) -> dict[str, Any]:
    """List the projects (sub-folders holding at least one Compose file) and their files.

    A folder that cannot be read is skipped, not fatal. The answer is kept within a byte
    budget; `truncated` says when projects were left out.
    """
    root_fd = safefs.open_dir(str(_root_path()))
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
                item = {"name": entry.name, "files": names}
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


OPERATIONS: dict[str, Operation] = {
    "projects.list": projects_list,
    "projects.file.read": file_read,
    "projects.file.write": file_write,
}
