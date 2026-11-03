"""Operations on Compose projects stored on the machine's disk.

The disk is the source of truth: this only reads and writes the Compose files of projects
that already live under ONE allowed folder (`ARMADOK_PROJECTS_DIR`). Hard rules:

- a project is a direct sub-folder of the allowed folder; a file is a Compose file of that
  folder, named like `compose.yaml` or `docker-compose.prod.yml`. Names are checked against
  strict patterns, so there is no path to traverse;
- every step works from an open directory handle (`dir_fd`) with `O_NOFOLLOW`, so a folder or
  file swapped for a symbolic link between a check and its use is refused, not followed;
- only regular files with a single link are touched; a FIFO or device named like a Compose file
  is refused without being opened for real (it would block the worker);
- a write must say which version it replaces (its SHA-256). The file is checked again just
  before it is replaced; a file created in the meantime is never overwritten. This narrows the
  race with other programs writing the same file but cannot close it completely: a rename cannot
  be made conditional on the old content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from armadok.worker.operation import Operation, OperationError

PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
COMPOSE_FILE = re.compile(r"(?:docker-)?compose(?:\.[A-Za-z0-9_-]{1,30})?\.ya?ml")
SHA256 = re.compile(r"[0-9a-f]{64}")
TEMP_FILE = re.compile(r"\.(?:docker-)?compose.*\.armadok-[0-9a-f]{16}\.tmp")
# JSON may spell one byte as up to six ("\u0000"), and a message is capped at 1 MiB: 128 KiB of
# content always fits, with room for the rest of the message.
MAX_FILE_BYTES = 128 * 1024
MAX_PROJECTS = 500
MAX_FILES_PER_PROJECT = 50
LIST_BUDGET_BYTES = 800 * 1024
STALE_TEMP_SECONDS = 600

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _root_path() -> Path:
    """Return the allowed folder (absolute, not `/`), or fail if none is configured."""
    configured = os.environ.get("ARMADOK_PROJECTS_DIR")
    if not configured:
        raise OperationError("worker.projects_not_configured")
    root = Path(configured)
    if not root.is_absolute() or root == Path("/") or not root.is_dir():
        raise OperationError("worker.projects_not_configured")
    return root


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    """Open a directory without following a symlink; map failures to worker codes."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise OperationError("worker.not_found") from None
    except OSError:
        # ELOOP (a symlink), ENOTDIR (not a folder), EACCES...
        raise OperationError("worker.forbidden") from None


class _Project:
    """An open project folder: use `.fd` for every operation inside it. Close when done."""

    def __init__(self, project: Any) -> None:
        if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
            raise OperationError("worker.invalid_path")
        root_fd = _open_dir(str(_root_path()))
        try:
            self.fd = _open_dir(project, root_fd)
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


def _stat_regular(info: os.stat_result) -> None:
    """Refuse anything but a regular file with a single link (no hard-link tricks)."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OperationError("worker.forbidden")


def _read(project_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    """Read a regular file without following symlinks and without blocking on special files.

    `O_NONBLOCK` keeps a FIFO from blocking the open; the type is checked right after.
    """
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=project_fd
        )
    except FileNotFoundError:
        raise OperationError("worker.not_found") from None
    except OSError:
        raise OperationError("worker.forbidden") from None
    try:
        # Checked on the raw descriptor, before it is wrapped as a file: a folder or a device
        # must be refused cleanly, not blow up inside the wrapper.
        info = os.fstat(fd)
        _stat_regular(info)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise OperationError("worker.too_large")
    return data, info


def projects_list(args: dict[str, Any]) -> dict[str, Any]:
    """List the projects (sub-folders holding at least one Compose file) and their files.

    A folder that cannot be read is skipped, not fatal. The answer is kept within a byte
    budget; `truncated` says when projects were left out.
    """
    root_fd = _open_dir(str(_root_path()))
    projects: list[dict[str, Any]] = []
    truncated = False
    used = 0
    try:
        with os.scandir(root_fd) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if not PROJECT_NAME.fullmatch(entry.name):
                    continue
                try:
                    project_fd = _open_dir(entry.name, root_fd)
                except OperationError:
                    continue  # a symlink, a plain file or an unreadable folder
                try:
                    with os.scandir(project_fd) as files:
                        names = sorted(
                            f.name
                            for f in files
                            if COMPOSE_FILE.fullmatch(f.name) and f.is_file(follow_symlinks=False)
                        )[:MAX_FILES_PER_PROJECT]
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
        data, _ = _read(project.fd, name)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OperationError("worker.not_text") from None
    return {"content": text, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _remove_stale_temps(project_fd: int) -> None:
    """Delete temporary files left behind by a crashed write (older than a few minutes)."""
    now = time.time()
    with os.scandir(project_fd) as entries:
        for entry in entries:
            if not TEMP_FILE.fullmatch(entry.name):
                continue
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime > STALE_TEMP_SECONDS:
                    os.unlink(entry.name, dir_fd=project_fd)
            except OSError:
                pass


def file_write(args: dict[str, Any]) -> dict[str, Any]:
    """Replace a Compose file (or create it), refusing if it changed since it was read.

    `expected_sha256` is the hash of the version being replaced, or null to create a file that
    must not exist yet.
    """
    name = _file_name(args.get("file"))
    content, expected = args.get("content"), args.get("expected_sha256")
    if not isinstance(content, str) or (
        expected is not None and not (isinstance(expected, str) and SHA256.fullmatch(expected))
    ):
        raise OperationError("worker.invalid_path")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise OperationError("worker.too_large")

    with _Project(args.get("project")) as project:
        fd = project.fd
        _remove_stale_temps(fd)

        before: os.stat_result | None = None
        try:
            current, before = _read(fd, name)
        except OperationError as error:
            if error.code != "worker.not_found":
                raise
            if expected is not None:
                raise OperationError("worker.conflict") from None  # it vanished since it was read
        else:
            if expected is None or hashlib.sha256(current).hexdigest() != expected:
                raise OperationError("worker.conflict")

        # Existing file: keep its owner and permission bits (never setuid and friends).
        # New file: private to its owner and group, because Compose files often hold secrets.
        mode = stat.S_IMODE(before.st_mode) & 0o777 if before else 0o640
        temp = f".{name}.armadok-{secrets.token_hex(8)}.tmp"
        tfd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=fd,
        )
        try:
            with os.fdopen(tfd, "wb") as handle:
                os.fchmod(handle.fileno(), mode)
                if before is not None:
                    try:
                        os.fchown(handle.fileno(), before.st_uid, before.st_gid)
                    except PermissionError:
                        pass  # not allowed to give it away: keep it ours
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if before is None:
                # link() fails if the name now exists: a file that appeared meanwhile is never
                # replaced.
                try:
                    os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    raise OperationError("worker.conflict") from None
                os.unlink(temp, dir_fd=fd)
            else:
                # Last look just before the swap: if the file changed while we prepared the
                # new one, give up rather than overwrite someone else's edit.
                try:
                    now = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise OperationError("worker.conflict") from None
                if (now.st_ino, now.st_size, now.st_mtime_ns) != (
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ):
                    raise OperationError("worker.conflict")
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=fd)
            except OSError:
                pass
            raise
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


OPERATIONS: dict[str, Operation] = {
    "projects.list": projects_list,
    "projects.file.read": file_read,
    "projects.file.write": file_write,
}
