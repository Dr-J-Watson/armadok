"""Command that restores a backup of the hub's own state: `python -m armadok.restore_state`.

Run it with the hub stopped (it refuses if the database is in use). It decrypts the backup with
`ARMADOK_STATE_BACKUP_KEY` or `ARMADOK_ENCRYPTION_KEY` (the key the backup was made with),
checks the database inside it (integrity, and that it does not come from a newer Armadok),
and only then writes the database and the identity key into the data folder. What was there is
not deleted: it is moved aside with a `.before-restore-<time>` suffix, **in clear text**, so a
wrong choice can be undone by hand and the old files should be removed once you are sure. The
key itself is never printed.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from armadok.core.config import Settings, load_settings
from armadok.core.errors import ApiError
from armadok.core.registry import load_registry
from armadok.main import all_migrations
from armadok.modules.enabled import ENABLED_MODULES
from armadok.modules.statebackup import archive
from armadok.modules.workers.identity import KEY_FILE


def _refuse_if_in_use(db: Path) -> None:
    """Refuse when something holds the current database (a running hub)."""
    if not db.exists():
        return
    conn = sqlite3.connect(db, timeout=0)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
    except sqlite3.OperationalError:
        raise ApiError("statebackup.hub_running", 409) from None
    finally:
        conn.close()


def _check_database(data: bytes) -> None:
    """Check the backed-up database: intact, and not from a newer Armadok than this one."""
    with tempfile.TemporaryDirectory(prefix="armadok-restore-") as scratch:
        path = Path(scratch) / "check.db"
        path.write_bytes(data)
        path.chmod(0o600)
        conn = sqlite3.connect(path)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ApiError("statebackup.corrupt", 422)
            known = {m.id for m in all_migrations(load_registry(ENABLED_MODULES))}
            stored = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
        except sqlite3.DatabaseError:
            raise ApiError("statebackup.corrupt", 422) from None
        finally:
            conn.close()
    if stored - known:
        raise ApiError("statebackup.newer_version", 422)


def _stage(target: Path, data: bytes) -> Path:
    """Write `data` next to `target` (mode 0600) and flush it to disk; return the staged path."""
    part = target.with_name(f".{target.name}.restore")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return part


def _sync_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def restore(settings: Settings, backup: Path) -> list[str]:
    """Restore `backup` into the data folder; return the names of the files written.

    Everything is checked and staged before anything is moved, so a refused or failed restore
    leaves the data folder as it was.
    """
    members = archive.unseal(settings, backup.read_bytes())
    _check_database(members[archive.DB_MEMBER])
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _refuse_if_in_use(settings.db_path)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    wanted = [(archive.DB_MEMBER, settings.db_path), (KEY_FILE, settings.data_dir / KEY_FILE)]
    staged = [(target, _stage(target, members[name])) for name, target in wanted if name in members]
    written = []
    try:
        for target, part in staged:
            # The old file is moved aside, not deleted. For the database that includes its
            # write-ahead files, which belong to the old database and must not be replayed.
            suffixes = ("", "-wal", "-shm") if target == settings.db_path else ("",)
            for suffix in suffixes:
                old = target.with_name(target.name + suffix)
                if old.exists():
                    old.rename(old.with_name(f"{old.name}.before-restore-{stamp}"))
            os.replace(part, target)
            written.append(target.name)
    finally:
        for _, part in staged:
            part.unlink(missing_ok=True)
    _sync_folder(settings.data_dir)
    return written


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the exit code."""
    parser = argparse.ArgumentParser(prog="armadok.restore_state", description=__doc__)
    parser.add_argument("backup", type=Path, help="the .tar.gz.enc file to restore")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    settings = load_settings()
    if not (settings.state_backup_key or settings.encryption_key):
        print(
            "ARMADOK_STATE_BACKUP_KEY or ARMADOK_ENCRYPTION_KEY is not set: "
            "the backup cannot be decrypted.",
            file=sys.stderr,
        )
        return 2
    if not args.yes:
        print(
            f"This replaces the data in {settings.data_dir} (the current files are kept aside).\n"
            "The hub must be stopped. Type 'restore' to continue: ",
            end="",
        )
        if input().strip() != "restore":
            return 1
    try:
        written = restore(settings, args.backup)
    except ApiError as error:
        print(f"Restore failed: {error.code}", file=sys.stderr)
        return 3
    except OSError as error:
        print(f"Restore failed: {error.strerror or 'file error'}", file=sys.stderr)
        return 4
    print("Restored: " + ", ".join(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
