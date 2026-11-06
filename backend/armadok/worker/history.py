"""Versions of Traefik configuration files, kept next to them on the node.

Before a file is replaced or removed, its previous text is copied to `.armadok-history/` at the
root of the Traefik folder, under a name that cannot be mistaken for configuration (`.bak`, and
Traefik reads only `.yml`/`.yaml`/`.toml`). Twenty versions per file are kept. Like every
other disk access of the worker, it never follows a link and never overwrites silently.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from typing import Any

from armadok.worker import safefs
from armadok.worker.operation import OperationError

HISTORY_DIR = ".armadok-history"
KEEP_VERSIONS = 20
MAX_BYTES = 128 * 1024
VERSION = re.compile(r"[0-9]{13}\.[0-9a-f]{8}")
MAX_TOTAL = 2000
_ANY_VERSION = re.compile(rf".+\.({VERSION.pattern})\.bak")


def _flat(path: str) -> str:
    """Return a file name for `path` (`~` cannot occur in a configuration path)."""
    return path.replace("/", "~")


def _pattern(path: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(_flat(path))}\.({VERSION.pattern})\.bak")


def _open_history(root_fd: int, create: bool) -> int:
    """Open the history folder, creating it when asked; refuse a link in its place."""
    if create:
        try:
            os.mkdir(HISTORY_DIR, 0o750, dir_fd=root_fd)
        except FileExistsError:
            pass
    return safefs.open_dir(HISTORY_DIR, root_fd)


def save(root_fd: int, path: str, data: bytes, *, strict: bool = False) -> None:
    """Keep `data` as a version of `path`.

    Best effort by default: a failure never fails the real change. With `strict` a failure
    raises `worker.history_failed`, for the one file where an unrecoverable change is worse than
    a refused one (Traefik's static configuration: a bad file stops the proxy).
    """
    try:
        hist = _open_history(root_fd, create=True)
    except (OSError, OperationError):
        if strict:
            raise OperationError("worker.history_failed") from None
        return
    try:
        name = f"{_flat(path)}.{int(time.time() * 1000):013d}.{secrets.token_hex(4)}.bak"
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o640,
            dir_fd=hist,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data[:MAX_BYTES])
        pattern = _pattern(path)
        mine = sorted(n for n in safefs.regular_names(hist, pattern))
        for old in mine[:-KEEP_VERSIONS]:
            os.unlink(old, dir_fd=hist)
        _prune_everything(hist)
    except OSError:
        if strict:
            raise OperationError("worker.history_failed") from None
    finally:
        os.close(hist)


def _prune_everything(hist: int) -> None:
    """Keep the whole folder bounded: drop the oldest versions beyond `MAX_TOTAL`.

    Per-file pruning alone lets histories of deleted files pile up for ever.
    """
    names = [n for n in safefs.regular_names(hist, _ANY_VERSION)]
    if len(names) <= MAX_TOTAL:
        return
    # The time is in the file name: sort on it, whatever the file the version belongs to.
    names.sort(key=lambda n: _ANY_VERSION.fullmatch(n).group(1))  # type: ignore[union-attr]
    for old in names[: len(names) - MAX_TOTAL]:
        os.unlink(old, dir_fd=hist)


def listing(root_fd: int, path: str) -> list[dict[str, Any]]:
    """Return the versions of `path`, newest first: `version`, `at` (ms since epoch), `size`."""
    try:
        hist = _open_history(root_fd, create=False)
    except OperationError:
        return []
    try:
        pattern = _pattern(path)
        found = []
        for name in safefs.regular_names(hist, pattern):
            match = pattern.fullmatch(name)
            assert match is not None
            size = os.stat(name, dir_fd=hist, follow_symlinks=False).st_size
            found.append({"version": match.group(1), "at": int(match.group(1)[:13]), "size": size})
        return sorted(found, key=lambda v: v["version"], reverse=True)
    finally:
        os.close(hist)


def read(root_fd: int, path: str, version: Any) -> bytes:
    """Return the text of one version of `path`."""
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise OperationError("worker.invalid_path")
    hist = _open_history(root_fd, create=False)
    try:
        data, _ = safefs.read_regular(hist, f"{_flat(path)}.{version}.bak", MAX_BYTES)
    finally:
        os.close(hist)
    return data
