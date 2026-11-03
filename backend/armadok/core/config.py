"""Application settings, read from environment variables.

Only names listed here are read; values are never logged. Defaults suit a
local run; production values come from the container environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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

    @property
    def db_path(self) -> Path:
        """Location of the single SQLite database."""
        return self.data_dir / "armadok.db"


def load_settings() -> Settings:
    """Build `Settings` from the `ARMADOK_*` environment variables listed in `.env.example`."""
    return Settings(
        data_dir=Path(os.environ.get("ARMADOK_DATA_DIR", "data")),
        cookie_secure=os.environ.get("ARMADOK_COOKIE_SECURE", "true").lower() != "false",
        encryption_key=os.environ.get("ARMADOK_ENCRYPTION_KEY") or None,
    )
