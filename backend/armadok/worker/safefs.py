"""Safe file access for the worker: no symlinks, no special files, no silent overwrites.

Shared by every operation that touches the machine's disk (Compose projects, Traefik files),
so the careful parts are written once:

- everything is done from open directory handles (`dir_fd`) with `O_NOFOLLOW`, so a folder or
  file swapped for a symbolic link between a check and its use is refused, not followed;
- only regular files with a single link are touched; a FIFO or device is refused without
  blocking (`O_NONBLOCK`) and a hard link is refused;
- a write names the version it replaces (SHA-256). The file is checked again just before it is
  replaced, and a file created in the meantime is never overwritten (`link()` fails if the
  name exists). A rename cannot be made conditional on the old content, so another program
  writing the same file at the very same moment can still win: the window is narrowed, not
  closed;
- a write goes through a random temporary file, keeps the original owner and permission bits
  (never setuid and friends) and gives new files `0640`, since these files often hold secrets.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator

from armadok.worker.operation import OperationError

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
STALE_TEMP_SECONDS = 600
SHA256 = re.compile(r"[0-9a-f]{64}")


def open_dir(name: str, dir_fd: int | None = None) -> int:
    """Open a directory without following a symlink; map failures to worker codes."""
    try:
        return os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise OperationError("worker.not_found") from None
    except OSError:
        # ELOOP (a symlink), ENOTDIR (not a folder), EACCES...
        raise OperationError("worker.forbidden") from None


def open_chain(root_fd: int, parts: list[str]) -> int:
    """Open `root/part1/part2/...` one step at a time, refusing a symlink at every step.

    Returns a new descriptor (the caller closes it); `root_fd` itself is left open.
    """
    current = os.dup(root_fd)
    try:
        for part in parts:
            nxt = open_dir(part, current)
            os.close(current)
            current = nxt
    except BaseException:
        os.close(current)
        raise
    return current


def stat_regular(info: os.stat_result) -> None:
    """Refuse anything but a regular file with a single link."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OperationError("worker.forbidden")


def read_regular(dir_fd: int, name: str, max_bytes: int) -> tuple[bytes, os.stat_result]:
    """Read a regular file; refuse links, special files and anything over `max_bytes`."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except FileNotFoundError:
        raise OperationError("worker.not_found") from None
    except OSError:
        raise OperationError("worker.forbidden") from None
    try:
        # Checked on the raw descriptor, before it is wrapped as a file: a folder or a device
        # must be refused cleanly, not blow up inside the wrapper.
        info = os.fstat(fd)
        stat_regular(info)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OperationError("worker.too_large")
    return data, info


def regular_names(dir_fd: int, pattern: re.Pattern[str]) -> Iterator[str]:
    """Yield names in a directory that match `pattern` and are regular files (no symlinks)."""
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if pattern.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                yield entry.name


def remove_stale_temps(
    dir_fd: int, temp_pattern: re.Pattern[str], max_age: float = STALE_TEMP_SECONDS
) -> None:
    """Delete temporary files left behind by a crashed write (older than `max_age` seconds)."""
    now = time.time()
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if not temp_pattern.fullmatch(entry.name):
                continue
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime >= max_age:
                    os.unlink(entry.name, dir_fd=dir_fd)
            except OSError:
                pass


def write_regular(
    dir_fd: int,
    name: str,
    data: bytes,
    expected_sha256: str | None,
    max_bytes: int,
    temp_pattern: re.Pattern[str],
    new_mode: int = 0o640,
) -> dict[str, object]:
    """Replace a file (or create it), refusing if it changed since it was read.

    `expected_sha256` is the hash of the version being replaced, or None to create a file
    that must not exist yet (made with `new_mode`). `temp_pattern` must match the temporary
    names made here.
    """
    if len(data) > max_bytes:
        raise OperationError("worker.too_large")
    remove_stale_temps(dir_fd, temp_pattern)

    before: os.stat_result | None = None
    try:
        current, before = read_regular(dir_fd, name, max_bytes)
    except OperationError as error:
        if error.code != "worker.not_found":
            raise
        if expected_sha256 is not None:
            raise OperationError("worker.conflict") from None  # it vanished since it was read
    else:
        if expected_sha256 is None or hashlib.sha256(current).hexdigest() != expected_sha256:
            raise OperationError("worker.conflict")

    mode = stat.S_IMODE(before.st_mode) & 0o777 if before else new_mode
    temp = f".{name}.armadok-{secrets.token_hex(8)}.tmp"
    tfd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
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
            try:
                os.link(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            except FileExistsError:
                raise OperationError("worker.conflict") from None
            os.unlink(temp, dir_fd=dir_fd)
        else:
            try:
                now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise OperationError("worker.conflict") from None
            if (now.st_ino, now.st_size, now.st_mtime_ns) != (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise OperationError("worker.conflict")
            os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
