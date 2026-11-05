"""What the hub accepts about Git: addresses from callers, answers from workers (untrusted)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The same rules as the worker: the hub never trusts a name just because a worker sent it.
URL_PATTERN = (
    r"^https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?"
    r"(/[A-Za-z0-9._~%+@=-]+){1,8}/?$"
)
BRANCH_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$"
HOST_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?$"
SHA_PATTERN = r"^([0-9a-f]{40}|[0-9a-f]{64})$"

_STRICT = ConfigDict(extra="ignore", strict=True)


def host_of(url: str) -> str:
    """Return the `host[:port]` of an address that matched `URL_PATTERN`, lower-cased."""
    return url.split("/", 3)[2].lower()


class Commit(BaseModel):
    """Answer of `git.clone` and `git.pull`."""

    model_config = _STRICT
    commit: str = Field(pattern=SHA_PATTERN)
    subject: str = Field(max_length=200)


class Status(BaseModel):
    """Answer of `git.status`: where the project is, and where the branch is now."""

    model_config = _STRICT
    commit: str = Field(pattern=SHA_PATTERN)
    subject: str = Field(max_length=200)
    dirty: bool
    remote: str = Field(pattern=SHA_PATTERN)
