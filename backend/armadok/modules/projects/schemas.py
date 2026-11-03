"""What the hub accepts from a worker about projects (untrusted), and from callers (checked)."""

from __future__ import annotations

import hashlib
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Same patterns as the worker: the hub never trusts a name just because a worker sent it.
PROJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
FILE_PATTERN = r"^(?:docker-)?compose(?:\.[A-Za-z0-9_-]{1,30})?\.ya?ml$"
SHA_PATTERN = r"^[0-9a-f]{64}$"
MAX_FILE_BYTES = 128 * 1024

_STRICT = ConfigDict(extra="ignore", strict=True)


class Project(BaseModel):
    """A project folder and its Compose files."""

    model_config = _STRICT
    name: str = Field(pattern=PROJECT_PATTERN)
    files: list[Annotated[str, StringConstraints(pattern=FILE_PATTERN, max_length=64)]] = Field(
        max_length=50
    )


class ProjectList(BaseModel):
    """Answer of `projects.list`."""

    model_config = _STRICT
    projects: list[Project] = Field(max_length=500)
    truncated: bool = False


class FileContent(BaseModel):
    """Answer of `projects.file.read`."""

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


class Saved(BaseModel):
    """Answer of `projects.file.write`."""

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


class Started(BaseModel):
    """Answer of `compose.start`: the worker's task id."""

    model_config = _STRICT
    task: str = Field(pattern=r"^[0-9a-f]{16}$")


class TaskState(BaseModel):
    """Answer of `compose.task`. The output is a bounded tail of what the command printed."""

    model_config = _STRICT
    project: str = Field(pattern=PROJECT_PATTERN)
    action: str = Field(max_length=20)
    state: str = Field(pattern="^(running|success|failed)$")
    exit_code: int | None = None
    output: str = Field(max_length=70 * 1024)
    truncated: bool = False


class ServiceState(BaseModel):
    """One container of a project, as shown to people."""

    model_config = _STRICT
    service: str = Field(max_length=64)
    name: str = Field(max_length=128)
    state: str = Field(max_length=30)
    status: str = Field(max_length=200)
    image: str = Field(max_length=300)


class ProjectStatus(BaseModel):
    """Answer of `compose.ps`."""

    model_config = _STRICT
    services: list[ServiceState] = Field(max_length=200)


class ProjectLogs(BaseModel):
    """Answer of `compose.logs`."""

    model_config = _STRICT
    output: str = Field(max_length=140 * 1024)
    truncated: bool = False


class ComposeCheck(BaseModel):
    """Answer of `compose.check`."""

    model_config = _STRICT
    valid: bool
    errors: str = Field(max_length=4200)


class ActionIn(BaseModel):
    """An action to run on a project. Flags only apply to the actions they make sense for."""

    action: str = Field(pattern="^(up|down|stop|start|restart|pull|build)$")
    files: list[str] | None = Field(default=None, max_length=5)
    service: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    build: bool = False
    pull: bool = False

    @model_validator(mode="after")
    def valid_files(self) -> ActionIn:
        """Compose files are names in the project folder, checked like everywhere else."""
        import re

        if self.files is not None and not (
            self.files and all(re.fullmatch(FILE_PATTERN, f) for f in self.files)
        ):
            raise ValueError("invalid compose file name")
        return self
