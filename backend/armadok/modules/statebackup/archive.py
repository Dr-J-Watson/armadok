"""Making, listing and removing backups of the hub's own state.

A backup holds a consistent snapshot of the database (taken with SQLite's online backup, so the
hub keeps running) and the hub's identity key, packed and then **encrypted with the backup key**
(`ARMADOK_STATE_BACKUP_KEY` when set, else the encryption key): a copied file is useless without
it, and restoring needs the same key (keep it elsewhere; it is the one thing a backup cannot
contain). Without a key no backup is made, because the identity key would otherwise sit in clear
text.

Files are written to a private folder (mode 0700) as 0600 files, never overwriting an existing
one, and only the newest few are kept.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from armadok.core.config import Settings
from armadok.core.errors import ApiError
from armadok.modules.workers.identity import KEY_FILE

NAME = re.compile(r"armadok-state-(?P<stamp>\d{8}T\d{6}Z)\.tar\.gz\.enc")
FORMAT = 1
DB_MEMBER = "armadok.db"
MANIFEST = "manifest.json"
MAX_LISTED = 500
# Bigger than this and the backup is refused: a backup is held in memory several times over
# (database, archive, compressed, encrypted), so an unbounded one could take the hub down.
MAX_DB_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = MAX_DB_BYTES + 1024 * 1024
FUTURE_GRACE = timedelta(days=1)  # a stamp further ahead than this is not ours
_creating = threading.Lock()


def folder(settings: Settings) -> Path:
    """Return the backup folder, created private if needed."""
    path = settings.state_backup_dir or settings.data_dir / "backups"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.stat().st_mode & 0o077 and path.stat().st_uid == os.getuid():
            path.chmod(0o700)  # a folder that already existed may be more open than we want
    except OSError:
        pass
    return path


def _fernets(settings: Settings) -> list[Fernet]:
    """Return the keys a backup may be sealed with, the one to use now first.

    That is the dedicated backup key when there is one, then the main encryption key (which also
    opens backups made before a dedicated key was set).
    """
    keys = [k for k in (settings.state_backup_key, settings.encryption_key) if k]
    if not keys:
        raise ApiError("statebackup.no_encryption_key", 409)
    try:
        return [Fernet(k.encode()) for k in keys]
    except ValueError:
        raise ApiError("statebackup.no_encryption_key", 409) from None


def _fernet(settings: Settings) -> Fernet:
    return _fernets(settings)[0]


def _snapshot(settings: Settings) -> bytes:
    """Return a consistent copy of the database, taken in memory.

    Nothing is written to disk in clear text: a crash half-way leaves nothing behind.
    """
    if settings.db_path.stat().st_size > MAX_DB_BYTES:
        raise ApiError("statebackup.too_large", 413)
    source = sqlite3.connect(settings.db_path)
    try:
        copy = sqlite3.connect(":memory:")
        try:
            source.backup(copy)
            return copy.serialize()
        finally:
            copy.close()
    finally:
        source.close()


def _sweep(where: Path) -> None:
    """Remove what an interrupted run of an earlier version could have left in the folder."""
    for entry in where.iterdir():
        if entry.name.startswith(".work-") and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        elif entry.name.startswith(".armadok-state-") and entry.name.endswith(".part"):
            entry.unlink(missing_ok=True)


def _pack(settings: Settings) -> bytes:
    """Build the (not yet encrypted) archive."""
    buffer = io.BytesIO()
    now = datetime.now(UTC)
    manifest = json.dumps({"format": FORMAT, "created": now.isoformat()}).encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        members: list[tuple[str, bytes]] = [
            (MANIFEST, manifest),
            (DB_MEMBER, _snapshot(settings)),
        ]
        key_path = settings.data_dir / KEY_FILE
        if key_path.is_file():
            members.append((KEY_FILE, key_path.read_bytes()))
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), int(now.timestamp()), 0o600
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def create(settings: Settings) -> str:
    """Make a backup now and return its file name; older ones beyond the limit are removed."""
    fernet = _fernet(settings)
    where = folder(settings)
    # One backup at a time: two at once would each hold the whole database in memory.
    if not _creating.acquire(blocking=False):
        raise ApiError("statebackup.too_soon", 409)
    try:
        _sweep(where)
        sealed = fernet.encrypt(_pack(settings))
        return _write(settings, where, sealed)
    finally:
        _creating.release()


def _write(settings: Settings, where: Path, sealed: bytes) -> str:
    """Write the encrypted backup under a new name (never replacing one) and rotate."""
    name = f"armadok-state-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.tar.gz.enc"
    part = where / f".{name}.part"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(sealed)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(part, where / name)  # fails if the name exists: never replace a backup
        except FileExistsError:
            raise ApiError("statebackup.too_soon", 409) from None
    finally:
        part.unlink(missing_ok=True)
    rotate(settings)
    return name


def listing(settings: Settings) -> list[dict[str, Any]]:
    """Return the backups, newest first (regular files with a well-formed name only)."""
    return _all(settings)[:MAX_LISTED]


def _all(settings: Settings) -> list[dict[str, Any]]:
    """Return every backup, newest first.

    A stamp in the far future is ignored: it would make a planted file look like the newest
    backup and push the real ones out of the rotation.
    """
    found = []
    limit = (datetime.now(UTC) + FUTURE_GRACE).strftime("%Y%m%dT%H%M%SZ")
    for entry in os.scandir(folder(settings)):
        match = NAME.fullmatch(entry.name)
        if not match or match["stamp"] > limit:
            continue
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            found.append({"name": entry.name, "stamp": match["stamp"], "size": info.st_size})
    found.sort(key=lambda b: b["stamp"], reverse=True)
    return found


def rotate(settings: Settings) -> None:
    """Keep the newest `state_backup_keep` backups and remove the rest (however many there are)."""
    for old in _all(settings)[settings.state_backup_keep :]:
        (folder(settings) / old["name"]).unlink(missing_ok=True)


def path_of(settings: Settings, name: str) -> Path:
    """Return the path of a listed backup, or raise `statebackup.not_found`."""
    if not NAME.fullmatch(name) or not any(b["name"] == name for b in listing(settings)):
        raise ApiError("statebackup.not_found", 404)
    return folder(settings) / name


def remove(settings: Settings, name: str) -> None:
    """Delete one backup."""
    path_of(settings, name).unlink(missing_ok=True)


def unseal(settings: Settings, data: bytes) -> dict[str, bytes]:
    """Decrypt and unpack a backup into `{member name: bytes}`; used by the restore command."""
    raw = None
    for fernet in _fernets(settings):
        try:
            raw = fernet.decrypt(data)
            break
        except InvalidToken:
            continue
    if raw is None:
        raise ApiError("statebackup.wrong_key", 422)
    out: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for member in tar.getmembers():
                # Only the three files we write, as plain files, of a sane size: nothing else
                # is ever extracted.
                if (
                    member.name in (MANIFEST, DB_MEMBER, KEY_FILE)
                    and member.isfile()
                    and member.size <= MAX_MEMBER_BYTES
                ):
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        out[member.name] = extracted.read(MAX_MEMBER_BYTES + 1)
        manifest = json.loads(out.get(MANIFEST, b"{}"))
        valid = isinstance(manifest, dict) and manifest.get("format") == FORMAT
    except (tarfile.TarError, EOFError, OSError, ValueError, AttributeError):
        raise ApiError("statebackup.invalid", 422) from None
    if not valid or DB_MEMBER not in out:
        raise ApiError("statebackup.invalid", 422)
    return out
