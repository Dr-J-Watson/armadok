"""What the hub accepts from a worker about certificates (untrusted)."""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

_STRICT = ConfigDict(extra="ignore", strict=True)
Name = Annotated[str, StringConstraints(max_length=253)]


class Certificate(BaseModel):
    """Public facts about one certificate. There is deliberately no field for a key."""

    model_config = ConfigDict(extra="ignore")  # dates arrive as ISO text, parsed here
    resolver: str = Field(max_length=60)
    names: list[Name] = Field(max_length=100)
    issuer: str = Field(max_length=200)
    not_before: AwareDatetime
    not_after: AwareDatetime
    serial: str = Field(pattern=r"^[0-9a-f]{1,64}$")


class CertificateList(BaseModel):
    """Answer of `certs.list`."""

    model_config = ConfigDict(extra="ignore")
    certificates: list[Certificate] = Field(max_length=1000)
    truncated: bool = False
