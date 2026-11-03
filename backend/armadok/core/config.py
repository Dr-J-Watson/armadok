"""Application settings, read from environment variables.

Only names listed here are read; values are never logged. Defaults suit a
local run; production values come from the container environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime settings of the hub."""

    # Folder holding the database and other state (a volume in production).
    data_dir: Path

    @property
    def db_path(self) -> Path:
        """Location of the single SQLite database."""
        return self.data_dir / "armadok.db"


def load_settings() -> Settings:
    """Build `Settings` from the environment (`ARMADOK_DATA_DIR`, default `./data`)."""
    return Settings(data_dir=Path(os.environ.get("ARMADOK_DATA_DIR", "data")))
