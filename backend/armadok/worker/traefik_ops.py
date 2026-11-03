"""Operations on Traefik's dynamic configuration folder (file provider).

Works inside ONE allowed folder (`ARMADOK_TRAEFIK_DIR`), up to four levels of sub-folders, on
files named `*.yml` / `*.yaml`. All disk access goes through `safefs` (no symlinks, no special
files, no silent overwrite). After saving a file in a sub-folder it refreshes `reload.txt` at the
root, because Traefik does not always notice changes in sub-folders by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from armadok.worker import safefs
from armadok.worker.operation import Operation, OperationError

DIR_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.ya?ml")
TEMP_FILE = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.ya?ml\.armadok-[0-9a-f]{16}\.tmp")
MAX_DEPTH = 4  # folders below the root
MAX_FILE_BYTES = 128 * 1024
MAX_FILES = 500
# Entries looked at in total, so a huge or deeply nested tree cannot keep the worker busy.
MAX_ENTRIES_SCANNED = 20000
SNAPSHOT_BUDGET_BYTES = 600 * 1024
RELOAD_FILE = "reload.txt"


def _root_path() -> Path:
    configured = os.environ.get("ARMADOK_TRAEFIK_DIR")
    if not configured:
        raise OperationError("worker.traefik_not_configured")
    root = Path(configured)
    if not root.is_absolute() or root == Path("/") or not root.is_dir():
        raise OperationError("worker.traefik_not_configured")
    return root


def _split(path: Any) -> tuple[list[str], str]:
    """Split `a/b/file.yml` into folders and file name, checking every part."""
    if not isinstance(path, str):
        raise OperationError("worker.invalid_path")
    *folders, name = path.split("/")
    if (
        len(folders) > MAX_DEPTH
        or not FILE_NAME.fullmatch(name)
        or not all(DIR_NAME.fullmatch(f) for f in folders)
    ):
        raise OperationError("worker.invalid_path")
    return folders, name


def _walk(dir_fd: int, prefix: str, depth: int, out: list[dict[str, Any]], seen: list[int]) -> None:
    """Collect YAML files below `dir_fd`, depth-limited; unreadable folders are skipped."""
    with os.scandir(dir_fd) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            seen[0] += 1
            if len(out) >= MAX_FILES or seen[0] > MAX_ENTRIES_SCANNED:
                return
            if (
                entry.is_dir(follow_symlinks=False)
                and depth < MAX_DEPTH
                and DIR_NAME.fullmatch(entry.name)
            ):
                try:
                    child = safefs.open_dir(entry.name, dir_fd)
                except OperationError:
                    continue
                try:
                    _walk(child, f"{prefix}{entry.name}/", depth + 1, out, seen)
                except OSError:
                    pass
                finally:
                    os.close(child)
            elif FILE_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                out.append(
                    {"path": prefix + entry.name, "size": entry.stat(follow_symlinks=False).st_size}
                )


def files_list(args: dict[str, Any]) -> dict[str, Any]:
    """List the configuration files of the folder."""
    root_fd = safefs.open_dir(str(_root_path()))
    files: list[dict[str, Any]] = []
    seen = [0]
    try:
        _walk(root_fd, "", 0, files, seen)
    finally:
        os.close(root_fd)
    return {"files": files, "truncated": len(files) >= MAX_FILES or seen[0] > MAX_ENTRIES_SCANNED}


def _open_parent(folders: list[str]) -> tuple[int, int]:
    """Return `(root_fd, parent_fd)`; the caller closes both."""
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        return root_fd, safefs.open_chain(root_fd, folders)
    except BaseException:
        os.close(root_fd)
        raise


def file_read(args: dict[str, Any]) -> dict[str, Any]:
    """Return a file's text and its SHA-256."""
    folders, name = _split(args.get("path"))
    root_fd, parent_fd = _open_parent(folders)
    try:
        data, _ = safefs.read_regular(parent_fd, name, MAX_FILE_BYTES)
    finally:
        os.close(parent_fd)
        os.close(root_fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OperationError("worker.not_text") from None
    return {"content": text, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _touch_reload(root_fd: int) -> None:
    """Make Traefik re-read the folder: best effort, never fails the save."""
    try:
        # No O_TRUNC here: the file is truncated only after it is known to be an ordinary file
        # with a single link, so a planted hard link or special file is never emptied.
        fd = os.open(
            RELOAD_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o640,
            dir_fd=root_fd,
        )
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            os.ftruncate(fd, 0)
            os.write(fd, f"{time.time()}\n".encode())
    finally:
        os.close(fd)


def file_write(args: dict[str, Any]) -> dict[str, Any]:
    """Replace a file (or create it), refusing if it changed since it was read."""
    folders, name = _split(args.get("path"))
    content, expected = args.get("content"), args.get("expected_sha256")
    if not isinstance(content, str) or (
        expected is not None
        and not (isinstance(expected, str) and safefs.SHA256.fullmatch(expected))
    ):
        raise OperationError("worker.invalid_path")
    root_fd, parent_fd = _open_parent(folders)
    try:
        result = safefs.write_regular(
            parent_fd, name, content.encode("utf-8"), expected, MAX_FILE_BYTES, TEMP_FILE
        )
        if folders:
            _touch_reload(root_fd)
        return result
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def snapshot(args: dict[str, Any]) -> dict[str, Any]:
    """Return every file's text in one answer (for the sites view and the relations map).

    Within a byte budget; files that do not fit are left out and `truncated` is set.
    """
    listing = files_list({})
    files: list[dict[str, Any]] = []
    used = 0
    truncated = listing["truncated"]
    for item in listing["files"]:
        try:
            one = file_read({"path": item["path"]})
        except OperationError:
            continue  # vanished or refused since the listing: skip, do not fail the whole view
        used += len(json.dumps(one["content"]))
        if used > SNAPSHOT_BUDGET_BYTES:
            truncated = True
            break
        files.append({"path": item["path"], **one})
    return {"files": files, "truncated": truncated}


OPERATIONS: dict[str, Operation] = {
    "traefik.files.list": files_list,
    "traefik.file.read": file_read,
    "traefik.file.write": file_write,
    "traefik.snapshot": snapshot,
}
