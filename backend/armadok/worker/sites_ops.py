"""Site files of Traefik: one generated file per site, in `sites/` below the Traefik folder.

A site is paused by renaming `<name>.yml` to `<name>.yml.paused`: Traefik reads only YAML and
TOML files, so it forgets the site, and the file (with its history) stays for resuming.
Everything goes through `safefs` (no links followed, no silent overwrite).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from typing import Any

from armadok.worker import history, safefs
from armadok.worker.operation import Operation, OperationError
from armadok.worker.traefik_ops import (
    MAX_FILE_BYTES,
    TEMP_FILE,
    _root_path,
    _split,
    _touch_reload,
)

SITES_DIR = "sites"
SITE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SITE_FILE = re.compile(r"([a-z0-9][a-z0-9-]{0,39})\.yml(\.paused)?")
MAX_SITES = 200
BUDGET_BYTES = 600 * 1024


def _name(value: Any) -> str:
    if not isinstance(value, str) or not SITE_NAME.fullmatch(value):
        raise OperationError("worker.invalid_path")
    return value


def _sites_dir(root_fd: int, create: bool) -> int:
    """Open `sites/` (creating it when asked)."""
    if create:
        try:
            os.mkdir(SITES_DIR, 0o750, dir_fd=root_fd)
        except FileExistsError:
            pass
    return safefs.open_dir(SITES_DIR, root_fd)


def sites_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return the site files (active and paused) with their text, within a byte budget."""
    root_fd = safefs.open_dir(str(_root_path()))
    files: list[dict[str, Any]] = []
    used = 0
    truncated = False
    try:
        try:
            sites = _sites_dir(root_fd, create=False)
        except OperationError as error:
            if error.code == "worker.not_found":
                return {"files": [], "truncated": False}
            raise
        try:
            for entry in sorted(safefs.regular_names(sites, SITE_FILE)):
                try:
                    data, _ = safefs.read_regular(sites, entry, MAX_FILE_BYTES)
                    text = data.decode("utf-8")
                except (OperationError, UnicodeDecodeError):
                    continue
                used += len(json.dumps(text))
                if len(files) >= MAX_SITES or used > BUDGET_BYTES:
                    truncated = True
                    break
                match = SITE_FILE.fullmatch(entry)
                assert match is not None
                files.append(
                    {
                        "name": match.group(1),
                        "paused": bool(match.group(2)),
                        "content": text,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                )
        finally:
            os.close(sites)
    finally:
        os.close(root_fd)
    return {"files": files, "truncated": truncated}


def site_write(args: dict[str, Any]) -> dict[str, Any]:
    """Create (no `expected_sha256`) or replace an active site file; keep the old version."""
    name = _name(args.get("name"))
    content, expected = args.get("content"), args.get("expected_sha256")
    if not isinstance(content, str) or (
        expected is not None
        and not (isinstance(expected, str) and safefs.SHA256.fullmatch(expected))
    ):
        raise OperationError("worker.invalid_path")
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        sites = _sites_dir(root_fd, create=True)
        try:
            if expected is None:
                if _exists(sites, f"{name}.yml.paused"):
                    raise OperationError("worker.conflict")  # a paused site has this name
                # Beyond the limit sites would be invisible to the form and to name checks.
                if sum(1 for _ in safefs.regular_names(sites, SITE_FILE)) >= MAX_SITES:
                    raise OperationError("worker.too_large")
            previous = _read_or_none(sites, f"{name}.yml")
            result = safefs.write_regular(
                sites, f"{name}.yml", content.encode("utf-8"), expected, MAX_FILE_BYTES, TEMP_FILE
            )
            if previous is not None:
                history.save(root_fd, f"{SITES_DIR}/{name}.yml", previous)
            _touch_reload(root_fd)
            return result
        finally:
            os.close(sites)
    finally:
        os.close(root_fd)


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _read_or_none(dir_fd: int, name: str) -> bytes | None:
    try:
        return safefs.read_regular(dir_fd, name, MAX_FILE_BYTES)[0]
    except OperationError as error:
        if error.code == "worker.not_found":
            return None
        raise


def _move(sites: int, source: str, target: str) -> None:
    """Rename without ever replacing: link the new name, then drop the old one."""
    safefs.read_regular(sites, source, MAX_FILE_BYTES)  # must be an ordinary file
    before = os.stat(source, dir_fd=sites, follow_symlinks=False)
    try:
        os.link(source, target, src_dir_fd=sites, dst_dir_fd=sites, follow_symlinks=False)
    except FileExistsError:
        raise OperationError("worker.conflict") from None
    except FileNotFoundError:
        raise OperationError("worker.not_found") from None
    try:
        # Whatever sits under the new name must be the ordinary file we just linked.
        after = os.stat(target, dir_fd=sites, follow_symlinks=False)
        # The very file we linked (same device and inode), not something put there since.
        if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise OperationError("worker.forbidden")
    except (OperationError, OSError):
        os.unlink(target, dir_fd=sites)
        raise OperationError("worker.forbidden") from None
    os.unlink(source, dir_fd=sites)
    os.fsync(sites)


def _switch(args: dict[str, Any], source_suffix: str, target_suffix: str) -> dict[str, Any]:
    name = _name(args.get("name"))
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        sites = _sites_dir(root_fd, create=False)
        try:
            _move(sites, f"{name}.yml{source_suffix}", f"{name}.yml{target_suffix}")
        finally:
            os.close(sites)
        _touch_reload(root_fd)
    finally:
        os.close(root_fd)
    return {"done": True}


def site_pause(args: dict[str, Any]) -> dict[str, Any]:
    """Stop Traefik from serving the site, keeping its file."""
    return _switch(args, "", ".paused")


def site_resume(args: dict[str, Any]) -> dict[str, Any]:
    """Serve a paused site again, exactly as it was."""
    return _switch(args, ".paused", "")


def site_delete(args: dict[str, Any]) -> dict[str, Any]:
    """Remove a site file (active or paused) if it still is the version the caller saw."""
    name = _name(args.get("name"))
    expected = args.get("expected_sha256")
    if not isinstance(expected, str) or not safefs.SHA256.fullmatch(expected):
        raise OperationError("worker.invalid_path")
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        sites = _sites_dir(root_fd, create=False)
        try:
            for suffix in ("", ".paused"):
                file_name = f"{name}.yml{suffix}"
                data = _read_or_none(sites, file_name)
                if data is None:
                    continue
                if hashlib.sha256(data).hexdigest() != expected:
                    raise OperationError("worker.conflict")
                history.save(root_fd, f"{SITES_DIR}/{name}.yml", data)
                os.unlink(file_name, dir_fd=sites)
                os.fsync(sites)
                _touch_reload(root_fd)
                return {"done": True}
            raise OperationError("worker.not_found")
        finally:
            os.close(sites)
    finally:
        os.close(root_fd)


def _history_target(args: dict[str, Any]) -> str:
    folders, name = _split(args.get("path"))
    return "/".join([*folders, name])


def history_list(args: dict[str, Any]) -> dict[str, Any]:
    """Return the kept versions of a configuration file, newest first."""
    path = _history_target(args)
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        return {"versions": history.listing(root_fd, path)}
    finally:
        os.close(root_fd)


def history_read(args: dict[str, Any]) -> dict[str, Any]:
    """Return the text of one kept version."""
    path = _history_target(args)
    root_fd = safefs.open_dir(str(_root_path()))
    try:
        data = history.read(root_fd, path, args.get("version"))
    finally:
        os.close(root_fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OperationError("worker.not_text") from None
    return {"content": text, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


OPERATIONS: dict[str, Operation] = {
    "traefik.sites.list": sites_list,
    "traefik.site.write": site_write,
    "traefik.site.pause": site_pause,
    "traefik.site.resume": site_resume,
    "traefik.site.delete": site_delete,
    "traefik.history.list": history_list,
    "traefik.history.read": history_read,
}
