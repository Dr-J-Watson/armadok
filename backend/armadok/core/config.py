"""Application settings, read from environment variables.

Only names listed here are read; values are never logged. Defaults suit a
local run; production values come from the container environment.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from armadok.core.db import Connection, connect


@dataclass(frozen=True)
class Settings:
    """Runtime settings of the hub."""

    # Folder holding the database and other state (a volume in production).
    data_dir: Path
    # Send the refresh cookie over HTTPS only. Disable only for local development over HTTP.
    cookie_secure: bool = True
    # Key that encrypts secrets stored in the database (Fernet, urlsafe base64, 32 bytes).
    # Kept apart from the database; never logged or returned by the API.
    encryption_key: str | None = field(default=None, repr=False)
    # Folder holding the built web interface. Absent in development (the dev server is used).
    web_dir: Path | None = None
    # Allow notification channels to reach private/loopback addresses (a self-hosted ntfy on the
    # LAN, say). Off by default: it lets whoever can add a channel make the hub call internal
    # services.
    allow_private_outbound: bool = False
    # Who a browser push says it comes from (an https address or `mailto:`), as the standard
    # asks. The default is the project's public page; set your own if you prefer.
    push_contact: str = "https://github.com/Dr-J-Watson/armadok"
    # Manage the hub's own node as a node, with no worker: the hub runs the operations itself.
    # Off by default, because it needs the hub to reach Docker and the configuration folders.
    local_node: bool = False
    # Where the hub's own backups go (default: a `backups` folder inside the data folder, which
    # is lost with the data: point it at another disk), how many to keep, and how often the
    # automatic backup runs (0 turns it off).
    # Look up the known vulnerabilities of Armadok's own components in the public OSV database
    # (only package names and versions are sent). Off by default: no call leaves the hub unless
    # the operator asks for it.
    vuln_check: bool = False
    state_backup_dir: Path | None = None
    # A key of its own for backups of the hub's state (so they open even if the main key is
    # lost). Without one, backups use `encryption_key`.
    state_backup_key: str | None = field(default=None, repr=False)
    state_backup_keep: int = 7
    state_backup_hours: int = 24

    @property
    def audit_key(self) -> bytes | None:
        """Key that signs audit entries (derived from the encryption key; None without one)."""
        if not self.encryption_key:
            return None
        return hashlib.sha256(b"armadok-audit-v1" + self.encryption_key.encode()).digest()

    def open_db(self) -> Connection:
        """Open the database with this configuration's audit signing key."""
        return connect(self.db_path, self.audit_key)

    @property
    def db_path(self) -> Path:
        """Location of the single SQLite database."""
        return self.data_dir / "armadok.db"


def _whole(raw: str | None, default: int, low: int, high: int) -> int:
    """Read an integer setting; anything missing or out of range falls back to the default."""
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if low <= value <= high else default


def load_settings() -> Settings:
    """Build `Settings` from the `ARMADOK_*` environment variables listed in `.env.example`."""
    return Settings(
        data_dir=Path(os.environ.get("ARMADOK_DATA_DIR", "data")),
        cookie_secure=os.environ.get("ARMADOK_COOKIE_SECURE", "true").lower() != "false",
        encryption_key=os.environ.get("ARMADOK_ENCRYPTION_KEY") or None,
        local_node=os.environ.get("ARMADOK_LOCAL_NODE", "").lower() == "true",
        allow_private_outbound=os.environ.get("ARMADOK_ALLOW_PRIVATE_OUTBOUND", "").lower()
        == "true",
        push_contact=os.environ.get("ARMADOK_PUSH_CONTACT")
        or "https://github.com/Dr-J-Watson/armadok",
        vuln_check=os.environ.get("ARMADOK_VULN_CHECK", "").lower() == "true",
        state_backup_dir=(
            Path(os.environ["ARMADOK_STATE_BACKUP_DIR"])
            if os.environ.get("ARMADOK_STATE_BACKUP_DIR")
            else None
        ),
        state_backup_key=os.environ.get("ARMADOK_STATE_BACKUP_KEY") or None,
        state_backup_keep=_whole(os.environ.get("ARMADOK_STATE_BACKUP_KEEP"), 7, 1, 365),
        state_backup_hours=_whole(os.environ.get("ARMADOK_STATE_BACKUP_HOURS"), 24, 0, 24 * 30),
        web_dir=Path(os.environ["ARMADOK_WEB_DIR"]) if os.environ.get("ARMADOK_WEB_DIR") else None,
    )
