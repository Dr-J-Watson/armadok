"""What the hub accepts from a worker about Traefik files (untrusted), and from callers."""

from __future__ import annotations

import hashlib
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
PATH_PATTERN = rf"^(?:{NAME}/){{0,4}}{NAME}\.ya?ml$"
SHA_PATTERN = r"^[0-9a-f]{64}$"
MAX_FILE_BYTES = 128 * 1024
MAX_SNAPSHOT_CHARS = 700 * 1024

_STRICT = ConfigDict(extra="ignore", strict=True)
Path = Annotated[str, StringConstraints(pattern=PATH_PATTERN, max_length=330)]


class FileInfo(BaseModel):
    """A configuration file in the list."""

    model_config = _STRICT
    path: Path
    size: int = Field(ge=0, le=MAX_FILE_BYTES * 8)


class FileList(BaseModel):
    """Answer of `traefik.files.list`."""

    model_config = _STRICT
    files: list[FileInfo] = Field(max_length=500)
    truncated: bool = False


class FileContent(BaseModel):
    """Answer of `traefik.file.read`."""

    model_config = _STRICT
    content: str = Field(max_length=MAX_FILE_BYTES)
    sha256: str = Field(pattern=SHA_PATTERN)
    size: int = Field(ge=0, le=MAX_FILE_BYTES)

    @model_validator(mode="after")
    def hash_and_size_match_content(self) -> FileContent:
        """Recompute both on the hub: a worker cannot hand out a wrong version hash."""
        data = self.content.encode()
        if self.size != len(data) or self.sha256 != hashlib.sha256(data).hexdigest():
            raise ValueError("hash or size does not match the content")
        return self


class SnapshotFile(FileContent):
    """One file of a snapshot."""

    path: Path


class Snapshot(BaseModel):
    """Answer of `traefik.snapshot`."""

    model_config = _STRICT
    files: list[SnapshotFile] = Field(max_length=500)
    truncated: bool = False

    @model_validator(mode="after")
    def total_size_is_bounded(self) -> Snapshot:
        """Bound the work a worker can make the hub do with one answer."""
        if sum(len(f.content) for f in self.files) > MAX_SNAPSHOT_CHARS:
            raise ValueError("snapshot too large")
        return self


class Saved(BaseModel):
    """Answer of `traefik.file.write`."""

    model_config = _STRICT
    sha256: str = Field(pattern=SHA_PATTERN)
    size: int = Field(ge=0, le=MAX_FILE_BYTES)


class WriteIn(BaseModel):
    """A file save: the new text and the version it replaces (null = create the file)."""

    content: str = Field(max_length=MAX_FILE_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=SHA_PATTERN)

    @model_validator(mode="after")
    def fits_in_bytes(self) -> WriteIn:
        """Enforce the limit in bytes, not characters (accents and CJK take several)."""
        if len(self.content.encode()) > MAX_FILE_BYTES:
            raise ValueError("file too large")
        return self
