"""Per-node settings that move the worker's folders and files inside what its environment allows.

The environment of the worker (set by whoever installs it) names the **roots**: the folder of the
projects, the Traefik folder, the backup folder, the access-log and certificate files. A node's
settings, sent by the hub with every call, may only *narrow* a root: a sub-folder of a folder
root, or another file name next to a file root. Anything that is not a plain name, or that
resolves (through a link, say) outside the root, is refused. The settings are not remembered by
the worker: each call carries them, so the worker and the hub can never disagree, and a hub that
is gone leaves the roots as they were.
"""

from __future__ import annotations

import contextvars
import os
import re
import stat
import threading
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any, NamedTuple

from armadok.worker.operation import Operation, OperationError

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_DEPTH = 6
MAX_VALUE = 400


class Setting(NamedTuple):
    """One movable place: the environment variable that is its root, and what it names."""

    env: str
    kind: str  # "dir": a folder, narrowed to a sub-folder; "file": a file, renamed in its folder


SETTINGS: dict[str, Setting] = {
    "projects_dir": Setting("ARMADOK_PROJECTS_DIR", "dir"),
    "traefik_dir": Setting("ARMADOK_TRAEFIK_DIR", "dir"),
    "traefik_static_dir": Setting("ARMADOK_TRAEFIK_STATIC_DIR", "dir"),
    "backup_dir": Setting("ARMADOK_BACKUP_DIR", "dir"),
    "acme_file": Setting("ARMADOK_ACME_FILE", "file"),
    "accesslog_file": Setting("ARMADOK_TRAEFIK_ACCESSLOG", "file"),
}

# What the current call carries (set around each operation, per thread of execution).
_values: ContextVar[dict[str, str] | None] = ContextVar("armadok_node_config", default=None)


def _root(name: str) -> str | None:
    """Return the root the environment gives for a setting, or None when it gives none."""
    value = os.environ.get(SETTINGS[name].env, "")
    return value or None


# Names that make a folder a project: a narrowing may not pass through or end in one, because the
# containers of a project can write inside its folder and could swap a step of the path.
_PROJECT_MARKERS = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
# Folders no narrowing may touch, contain or sit under: the hub's own data (database, keys).
_protected: list[Path] = []


def protect(folder: str | Path) -> None:
    """Forbid narrowing to, into or over a folder (the local node's data folder, say)."""
    resolved = Path(folder).resolve()
    if resolved not in _protected:
        _protected.append(resolved)


def _protected_folders() -> list[Path]:
    found = list(_protected)
    for variable in ("ARMADOK_DATA_DIR", "ARMADOK_WORKER_DIR"):
        if os.environ.get(variable):
            found.append(Path(os.environ[variable]).resolve())
    return found


def _narrow(name: str, root: str, value: str) -> str:
    """Return the path a setting's value points to, or raise `ValueError`.

    A folder value is a relative path of plain names (at most `MAX_DEPTH`); a file value is one
    plain name with the root file's own extension. Every step must be a real folder (a link is
    refused, not followed), none may be a project folder, and the end may not touch the hub's
    data. Reasons: `invalid`, `outside`, `not_found`, `forbidden`.
    """
    kind = SETTINGS[name].kind
    try:
        if kind == "file":
            if not NAME.fullmatch(value) or Path(value).suffix != Path(root).suffix:
                raise ValueError("invalid")
            return str(Path(root).parent / value)
        parts = [p for p in value.split("/") if p != ""]
        if len(parts) > MAX_DEPTH or any(not NAME.fullmatch(p) for p in parts):
            raise ValueError("invalid")
        base = Path(root).resolve()
        current = base
        for part in parts:
            current = current / part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("outside")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("not_found")
            if any((current / marker).exists() for marker in _PROJECT_MARKERS):
                raise ValueError("forbidden")
        for guarded in _protected_folders():
            if current == guarded or guarded in current.parents or current in guarded.parents:
                raise ValueError("forbidden")
        return str(current)
    except FileNotFoundError:
        raise ValueError("not_found") from None
    except (OSError, RuntimeError):
        raise ValueError("invalid") from None


def effective(name: str) -> str | None:
    """Return the path to use for a setting now: the root, narrowed by this call's value.

    None when the environment gives no root. Raises `worker.config_invalid` when the call's
    value is not allowed (the hub checks first, so this is a safety net, not a normal path).
    """
    root = _root(name)
    if root is None:
        return None
    value = (_values.get() or {}).get(name, "")
    if not value:
        return root
    try:
        return _narrow(name, root, value)
    except ValueError:
        raise OperationError("worker.config_invalid") from None


def relative(name: str) -> str:
    """Return the narrowing in force for a folder setting ("" for the root itself)."""
    return "/".join(p for p in (_values.get() or {}).get(name, "").split("/") if p)


def scoped(handler: Operation) -> Operation:
    """Wrap an operation so the settings in its arguments apply to it and are not seen by it."""

    def run(args: dict[str, Any]) -> dict[str, Any]:
        carried = args.get("_config")
        clean = {
            k: v
            for k, v in (carried if isinstance(carried, dict) else {}).items()
            if k in SETTINGS and isinstance(v, str) and len(v) <= MAX_VALUE
        }
        token = _values.set(clean)
        try:
            return handler({k: v for k, v in args.items() if k != "_config"})
        finally:
            _values.reset(token)

    run.__doc__ = handler.__doc__
    return run


def start_thread(target: Callable[..., None], *args: Any) -> None:
    """Start a background thread that keeps the settings of the call that started it.

    A new thread does not inherit them: without this, a deployment or a backup would work in the
    root folder while the call that started it, and the rights checked for it, saw the narrowed one.
    """
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(target, *args), daemon=True).start()


def describe(args: dict[str, Any]) -> dict[str, Any]:
    """Say, for each movable place, whether the environment gives a root and what is in force."""
    out: dict[str, Any] = {}
    for name, setting in SETTINGS.items():
        root = _root(name)
        value = (_values.get() or {}).get(name, "")
        valid = True
        if root and value:
            try:
                _narrow(name, root, value)
            except ValueError:
                valid = False
        out[name] = {"kind": setting.kind, "root": root, "value": value, "valid": valid}
    return {"settings": out}


def check(args: dict[str, Any]) -> dict[str, Any]:
    """Test values the hub is about to keep; return `{name: reason}` for those that fail.

    Reasons: `not_configured` (the environment gives no root), `invalid` (not plain names),
    `outside` (leads out of the root or through a link), `not_found` (no such folder),
    `forbidden` (a project folder, or the hub's own data).
    """
    values = args.get("values")
    if not isinstance(values, dict):
        raise OperationError("worker.op_failed")
    errors: dict[str, str] = {}
    for name, value in values.items():
        if name not in SETTINGS or not isinstance(value, str) or len(value) > MAX_VALUE:
            errors[str(name)[:40]] = "invalid"
            continue
        if not value:
            continue  # putting a place back to its root is always possible
        root = _root(name)
        if root is None:
            errors[name] = "not_configured"
            continue
        try:
            _narrow(name, root, value)
        except ValueError as error:
            errors[name] = str(error)
    return {"errors": errors}


OPERATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "config.describe": describe,
    "config.check": check,
}
