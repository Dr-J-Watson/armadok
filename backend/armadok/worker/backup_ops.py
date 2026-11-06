"""Backing up and restoring Docker volumes, on the node that holds them.

A volume's files belong to whatever user the containers use, so reading them all needs more
than the worker's own rights. The work is done by a short-lived helper container, always the
same kind and never anything else the hub asks for:

- the image is the worker's own (it holds `sh` and `tar`), so nothing is downloaded;
- no network, a read-only root file system, no capability but the few that tar needs, a memory
  and process limit, and the label `armadok.protect` so no one stops it by mistake;
- the command is a fixed script; the only things that vary are names the worker checked
  against a strict pattern, passed as arguments (never put into the script text);
- a backup mounts the volume read-only; a restore mounts the backup folder read-only;
- archives are written into the one folder `ARMADOK_BACKUP_DIR` under umask 077, then handed to
  the folder's group (0640). The worker lists, names and deletes them, and reads one out only
  when a person asked for it (a fresh second-factor code, checked by the hub);
- a restore is refused while any container (running or not) uses the volume.

Long actions run in the background and are followed by task id, like Compose actions.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import stat
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from armadok.worker import nodeconfig, safefs
from armadok.worker.docker_control import PROTECT_LABEL, own_container_id
from armadok.worker.docker_ops import docker_get, docker_raw
from armadok.worker.operation import Operation, OperationError

VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ARCHIVE = re.compile(
    r"(?P<volume>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})--(?P<stamp>[0-9]{8}T[0-9]{6}Z)\.tar\.gz"
)
TASK_ID = re.compile(r"[0-9a-f]{16}")
MAX_ARCHIVES = 2000
DEADLINE_SECONDS = 2 * 3600
POLL_SECONDS = 1.0
MAX_RUNNING = 2
MAX_TASKS_KEPT = 50
HELPER_MEMORY = 512 * 1024 * 1024

# `$1` is the archive name. The name is never part of the script text.
# The archive is handed to the folder's own group (0640): that group already decides what is in
# the folder, and it is how the worker can read an archive to hand it to its owner on request.
_BACKUP_SCRIPT = (
    'umask 077 && tar -C /data -czf "/backup/$1" --numeric-owner --one-file-system . '
    '&& chgrp --reference=/backup "/backup/$1" && chmod 0640 "/backup/$1"'
)
_RESTORE_SCRIPT = 'exec tar -C /data -xzpf "/backup/$1" --numeric-owner'
# "Empty the volume first": the archive is read through once before anything is deleted, so a
# damaged archive never costs the volume's current content.
_RESTORE_CLEAR_SCRIPT = (
    'tar -tzf "/backup/$1" >/dev/null && find /data -mindepth 1 -delete '
    '&& exec tar -C /data -xzpf "/backup/$1" --numeric-owner'
)
_BACKUP_CAPS = ["DAC_READ_SEARCH", "DAC_OVERRIDE", "CHOWN"]
CHUNK_BYTES = 384 * 1024  # base64 of this still fits a worker message (1 MiB)
_RESTORE_CAPS = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "DAC_READ_SEARCH"]

_lock = threading.Lock()
_tasks: dict[str, dict[str, Any]] = {}


def _folder() -> str:
    configured = nodeconfig.effective("backup_dir")
    if not configured or not os.path.isabs(configured):
        raise OperationError("worker.backups_not_configured")
    return configured


def _open_folder() -> int:
    return safefs.open_dir(_folder())


def _volume(value: Any) -> str:
    if not isinstance(value, str) or not VOLUME.fullmatch(value):
        raise OperationError("worker.invalid_path")
    return value


def _archive(value: Any) -> str:
    if not isinstance(value, str) or not ARCHIVE.fullmatch(value):
        raise OperationError("worker.invalid_path")
    return value


def _refuse_own_folder(volume: str, source: str) -> None:
    if volume == source:
        raise OperationError("worker.forbidden")


def _host_side() -> tuple[str, str]:
    """Return `(own image id, how the daemon names our backup folder)`.

    The daemon, not this process, mounts the folder into the helper, so it needs the folder's
    host-side source. It is read from our own container's mounts, never from the hub.
    """
    own = own_container_id()
    if own is None:
        raise OperationError("worker.backups_not_configured")
    info = docker_get(f"/containers/{own}/json")
    if not isinstance(info, dict):
        raise OperationError("worker.docker_error")
    image = info.get("Image")
    target = _folder().rstrip("/")
    for mount in info.get("Mounts") or []:
        if isinstance(mount, dict) and str(mount.get("Destination", "")).rstrip("/") == target:
            source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
            if isinstance(image, str) and isinstance(source, str) and source:
                return image, source
    raise OperationError("worker.backups_not_configured")


def _spec(image: str, binds: list[str], script: str, caps: list[str], name: str) -> dict[str, Any]:
    """Build the one kind of container this module ever creates."""
    return {
        "Image": image,
        "User": "0:0",
        "Entrypoint": ["sh", "-c", script, "armadok-backup"],
        "Cmd": [name],
        "NetworkDisabled": True,
        "Labels": {PROTECT_LABEL: "true", "armadok.helper": "backup"},
        "HostConfig": {
            "Binds": binds,
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": caps,
            "SecurityOpt": ["no-new-privileges"],
            "Memory": HELPER_MEMORY,
            "PidsLimit": 128,
            "AutoRemove": False,
        },
    }


def _remove_helper(ident: str) -> None:
    """Remove a helper container; a failure here never hides the error that came before it.

    A helper that could not be removed is an orphan: `_cleanup_orphans` takes it away later.
    """
    try:
        docker_raw("DELETE", f"/containers/{ident}?force=1&v=0", 30)
    except OperationError:
        pass


def _cleanup_orphans() -> None:
    """Remove helpers and half-written archives left behind by a crash or a restart.

    Only done while no job of this process is running, so a live job's helper is never touched.
    A helper of a job that outlived a worker restart is removed too: that job is lost anyway
    (its state was in memory).
    """
    with _lock:
        if any(t["state"] == "running" for t in _tasks.values()):
            return
    filters = quote(json.dumps({"label": ["armadok.helper=backup"]}))
    try:
        status, body = docker_raw("GET", f"/containers/json?all=1&filters={filters}", 15)
        found = json.loads(body) if status == 200 else []
    except (OperationError, ValueError):
        found = []
    for item in found if isinstance(found, list) else []:
        ident = item.get("Id") if isinstance(item, dict) else None
        if isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{64}", ident):
            _remove_helper(ident)
    try:
        folder_fd = _open_folder()
    except OperationError:
        return
    try:
        for name in os.listdir(folder_fd):
            if name.startswith(".") and name.endswith(".part") and ARCHIVE.fullmatch(name[1:-5]):
                _remove_quietly(folder_fd, name)
    finally:
        os.close(folder_fd)


def _run_helper(spec: dict[str, Any]) -> int:
    """Create, start and wait for the helper; always remove it. Return its exit code."""
    status, body = docker_raw("POST", "/containers/create", 30, spec)
    if status != 201:
        raise OperationError("worker.docker_error")
    try:
        ident = json.loads(body).get("Id")
    except (ValueError, AttributeError):
        raise OperationError("worker.docker_error") from None
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{64}", ident):
        raise OperationError("worker.docker_error")
    try:
        status, _ = docker_raw("POST", f"/containers/{ident}/start", 30)
        if status not in (204, 304):
            raise OperationError("worker.docker_error")
        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            state = (docker_get(f"/containers/{ident}/json") or {}).get("State") or {}
            if not state.get("Running"):
                code = state.get("ExitCode")
                return code if type(code) is int else 1
            time.sleep(POLL_SECONDS)
        raise OperationError("worker.timeout")
    finally:
        _remove_helper(ident)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _finish(task_id: str, state: str, error: str | None, **extra: Any) -> None:
    with _lock:
        _tasks[task_id].update(state=state, error=error, finished=time.time(), **extra)


def _remove_quietly(folder_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=folder_fd)
    except OSError:
        pass


def _backup_work(task_id: str, volume: str, keep: int) -> None:
    folder_fd = None
    name = f"{volume}--{_stamp()}.tar.gz"
    part = f".{name}.part"
    try:
        image, source = _host_side()
        folder_fd = _open_folder()
        spec = _spec(
            image, [f"{volume}:/data:ro", f"{source}:/backup"], _BACKUP_SCRIPT, _BACKUP_CAPS, part
        )
        code = _run_helper(spec)
        if code != 0:
            _remove_quietly(folder_fd, part)
            _finish(task_id, "failed", "worker.backup_failed")
            return
        try:
            # A hard link fails if the name exists, unlike a rename: an archive is never replaced.
            os.link(part, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        except FileExistsError:
            raise OperationError("worker.conflict") from None
        os.unlink(part, dir_fd=folder_fd)
        size = os.stat(name, dir_fd=folder_fd, follow_symlinks=False).st_size
        _prune_old(folder_fd, volume, keep)
        _finish(task_id, "success", None, archive=name, size=size)
    except OperationError as error:
        if folder_fd is not None:
            _remove_quietly(folder_fd, part)
        _finish(task_id, "failed", error.code)
    except Exception:  # noqa: BLE001  (details stay on the node)
        if folder_fd is not None:
            _remove_quietly(folder_fd, part)
        _finish(task_id, "failed", "worker.op_failed")
    finally:
        if folder_fd is not None:
            os.close(folder_fd)


def _restore_work(task_id: str, volume: str, archive: str, clear: bool = False) -> None:
    try:
        image, source = _host_side()
        spec = _spec(
            image,
            [f"{volume}:/data", f"{source}:/backup:ro"],
            _RESTORE_CLEAR_SCRIPT if clear else _RESTORE_SCRIPT,
            _RESTORE_CAPS,
            archive,
        )
        code = _run_helper(spec)
        _finish(
            task_id,
            "success" if code == 0 else "failed",
            None if code == 0 else "worker.restore_failed",
        )
    except OperationError as error:
        _finish(task_id, "failed", error.code)
    except Exception:  # noqa: BLE001
        _finish(task_id, "failed", "worker.op_failed")


def _entries(folder_fd: int) -> list[dict[str, Any]]:
    """List the archives of the folder (regular files with a well-formed name), newest first."""
    return _scan(folder_fd)[0]


def _scan(folder_fd: int) -> tuple[list[dict[str, Any]], bool]:
    """Return the archives and whether the folder holds more than could be read."""
    found = []
    names = os.listdir(folder_fd)
    for name in names[: MAX_ARCHIVES * 4]:
        match = ARCHIVE.fullmatch(name)
        if not match:
            continue
        try:
            info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found.append(
                {
                    "name": name,
                    "volume": match["volume"],
                    "stamp": match["stamp"],
                    "size": info.st_size,
                }
            )
    found.sort(key=lambda e: e["stamp"], reverse=True)
    return found[:MAX_ARCHIVES], len(names) > MAX_ARCHIVES * 4 or len(found) > MAX_ARCHIVES


def _prune_old(folder_fd: int, volume: str, keep: int) -> None:
    """Keep the newest `keep` archives of one volume and delete the rest."""
    mine = [e for e in _entries(folder_fd) if e["volume"] == volume]
    for old in mine[keep:]:
        _remove_quietly(folder_fd, old["name"])


# --- operations ------------------------------------------------------------------------------


def _start(target: Any, *args: Any, volume: str) -> dict[str, Any]:
    with _lock:
        running = [t for t in _tasks.values() if t["state"] == "running"]
        if any(t["volume"] == volume for t in running):
            raise OperationError("worker.project_busy")
        if len(running) >= MAX_RUNNING:
            raise OperationError("worker.busy")
        task_id = secrets.token_hex(8)
        _tasks[task_id] = {
            "volume": volume,
            "state": "running",
            "error": None,
            "archive": None,
            "size": None,
            "started": time.time(),
            "finished": None,
        }
        for old in sorted(_tasks, key=lambda k: _tasks[k]["started"])[:-MAX_TASKS_KEPT]:
            if _tasks[old]["state"] != "running":
                del _tasks[old]
    nodeconfig.start_thread(target, task_id, *args)
    return {"task": task_id}


def _check_volume(volume: str, must_exist: bool) -> None:
    """Refuse volumes that are not plain local ones, or that are not what they seem.

    A local volume created with `type=none` and `o=bind` is a window onto a host folder: a
    helper running as root over it would read or write that folder. The backup folder's own
    volume is refused too, so backups never swallow themselves.
    """
    status, body = docker_raw("GET", f"/volumes/{quote(volume, safe='')}", 15)
    if status == 404:
        if must_exist:
            raise OperationError("worker.not_found")
        return  # a restore may create the volume: Docker makes a plain local one on first use
    try:
        info = json.loads(body) if status == 200 else None
    except ValueError:
        info = None
    if not isinstance(info, dict) or info.get("Driver") != "local":
        raise OperationError("worker.not_found")
    options = info.get("Options") or {}
    if isinstance(options, dict) and (
        options.get("type") == "none" or "bind" in str(options.get("o", ""))
    ):
        raise OperationError("worker.forbidden")


def backup_start(args: dict[str, Any]) -> dict[str, Any]:
    """Start a backup of one local volume. Inputs: `volume`, `keep` (archives to keep)."""
    volume, keep = _volume(args.get("volume")), args.get("keep", 7)
    if type(keep) is not int or not 1 <= keep <= 365:
        raise OperationError("worker.invalid_path")
    _, source = _host_side()  # fail now, with the right code, if backups cannot work here
    _refuse_own_folder(volume, source)
    _check_volume(volume, must_exist=True)
    _cleanup_orphans()
    return _start(_backup_work, volume, keep, volume=volume)


def restore_start(args: dict[str, Any]) -> dict[str, Any]:
    """Restore an archive into a volume nobody uses.

    Inputs: `archive`, `volume`, `clear: true` to empty the volume before restoring, and
    `other: true` to restore into a volume that is not the one the archive was made from (mixing
    data up by mistake is the risk the flag guards against).
    """
    archive, volume = _archive(args.get("archive")), _volume(args.get("volume"))
    other, clear = args.get("other", False), args.get("clear", False)
    if not isinstance(other, bool) or not isinstance(clear, bool):
        raise OperationError("worker.invalid_path")
    if not other and ARCHIVE.fullmatch(archive)["volume"] != volume:  # type: ignore[index]
        raise OperationError("worker.conflict")
    folder_fd = _open_folder()
    try:
        if not any(e["name"] == archive for e in _entries(folder_fd)):
            raise OperationError("worker.not_found")
    finally:
        os.close(folder_fd)
    _, source = _host_side()
    _refuse_own_folder(volume, source)
    _check_volume(volume, must_exist=False)
    _cleanup_orphans()
    filters = quote(json.dumps({"volume": [volume]}))
    status, body = docker_raw("GET", f"/containers/json?all=1&filters={filters}", 15)
    if status != 200:
        raise OperationError("worker.docker_error")
    try:
        users = json.loads(body)
    except ValueError:
        raise OperationError("worker.docker_error") from None
    if not isinstance(users, list) or users:
        raise OperationError("worker.conflict")
    return _start(_restore_work, volume, archive, clear, volume=volume)


def backup_task(args: dict[str, Any]) -> dict[str, Any]:
    """Return the state of a backup or restore started here."""
    task_id = args.get("task")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise OperationError("worker.invalid_path")
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            raise OperationError("worker.not_found")
        return {k: task[k] for k in ("volume", "state", "error", "archive", "size")}


def backup_list(args: dict[str, Any]) -> dict[str, Any]:
    """List the archives in the backup folder."""
    folder_fd = _open_folder()
    try:
        archives, truncated = _scan(folder_fd)
        return {"archives": archives, "truncated": truncated}
    finally:
        os.close(folder_fd)


def backup_read(args: dict[str, Any]) -> dict[str, Any]:
    """Return one piece of an archive, base64-encoded, with the archive's total size.

    Inputs: `archive` and `offset`. The caller fetches piece after piece; nothing is kept here.
    An archive made before archives were group-readable cannot be read: `worker.forbidden`.
    """
    archive, offset = _archive(args.get("archive")), args.get("offset")
    if type(offset) is not int or offset < 0:
        raise OperationError("worker.invalid_path")
    folder_fd = _open_folder()
    try:
        try:
            fd = os.open(
                archive,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=folder_fd,
            )
        except FileNotFoundError:
            raise OperationError("worker.not_found") from None
        except OSError:
            raise OperationError("worker.forbidden") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OperationError("worker.forbidden")
            data = os.pread(fd, CHUNK_BYTES, offset)
        finally:
            os.close(fd)
    finally:
        os.close(folder_fd)
    return {"data": base64.b64encode(data).decode("ascii"), "size": info.st_size}


def backup_delete(args: dict[str, Any]) -> dict[str, Any]:
    """Delete one archive by name."""
    archive = _archive(args.get("archive"))
    folder_fd = _open_folder()
    try:
        if not any(e["name"] == archive for e in _entries(folder_fd)):
            raise OperationError("worker.not_found")
        os.unlink(archive, dir_fd=folder_fd)
    except OSError:
        raise OperationError("worker.forbidden") from None
    finally:
        os.close(folder_fd)
    return {"done": True}


OPERATIONS: dict[str, Operation] = {
    "backups.start": backup_start,
    "backups.restore": restore_start,
    "backups.task": backup_task,
    "backups.list": backup_list,
    "backups.delete": backup_delete,
    "backups.read": backup_read,
}
