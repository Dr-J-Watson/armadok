"""What the hub accepts from a worker about Docker.

Worker answers are untrusted: they are parsed into these strict models (types are not
coerced, every string and list has a limit) and anything that does not fit is refused as a
whole. Labels are filtered again with the same allow-list the worker uses, so a compromised
worker cannot make the API show more than a healthy one would.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from armadok.worker_protocol.docker_labels import filter_labels

Text = Annotated[str, StringConstraints(max_length=300)]
Path = Annotated[str, StringConstraints(max_length=1000)]
LabelKey = Annotated[str, StringConstraints(max_length=200)]
LabelValue = Annotated[str, StringConstraints(max_length=500)]

_STRICT = ConfigDict(extra="ignore", strict=True)


class Port(BaseModel):
    """A published or exposed port."""

    model_config = _STRICT
    private: int | None = Field(default=None, ge=0, le=65535)
    public: int | None = Field(default=None, ge=0, le=65535)
    type: str = Field(default="", max_length=10)
    ip: str = Field(default="", max_length=64)


class Mount(BaseModel):
    """A volume or bind mount."""

    model_config = _STRICT
    type: str = Field(default="", max_length=20)
    name: Text = ""
    source: Path = ""
    destination: Path = ""
    rw: bool = False


class Container(BaseModel):
    """A container as shown to users."""

    model_config = _STRICT
    id: str = Field(max_length=64)
    names: list[Text] = Field(default_factory=list, max_length=5)
    image: Text = ""
    state: str = Field(default="", max_length=30)
    status: Text = ""
    created: int = Field(default=0, ge=0)
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=100)
    ports: list[Port] = Field(default_factory=list, max_length=50)
    networks: list[Text] = Field(default_factory=list, max_length=20)
    mounts: list[Mount] = Field(default_factory=list, max_length=50)

    @field_validator("labels")
    @classmethod
    def only_allowed_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        """Drop any label outside the allow-list, whatever the worker sent."""
        return filter_labels(labels)


class ContainerList(BaseModel):
    """The answer of `docker.containers.list`."""

    model_config = _STRICT
    containers: list[Container] = Field(max_length=1000)
    truncated: bool = False


class DockerInfo(BaseModel):
    """The answer of `docker.info`."""

    model_config = _STRICT
    version: str = Field(max_length=50)
    api_version: str = Field(max_length=50)
