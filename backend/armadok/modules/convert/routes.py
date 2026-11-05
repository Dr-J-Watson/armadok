"""Endpoint of the converter: paste a `docker run` command, get a Compose file."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from armadok.core.deps import get_principal
from armadok.core.errors import ApiError
from armadok.core.rights import Principal
from armadok.modules.convert import dockerrun

router = APIRouter(prefix="/convert", tags=["convert"])


class DockerRunIn(BaseModel):
    """The command as it was pasted."""

    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=dockerrun.MAX_COMMAND)


@router.post("/docker-run", summary="Turn a docker run command into a Compose file")
def docker_run(body: DockerRunIn, who: Principal = Depends(get_principal)) -> dict:
    """Return the Compose text, a `.env` text for values that look like secrets, and warnings.

    The command is only read, never run, and nothing is stored. Needs a signed-in account.
    """
    try:
        return dict(dockerrun.convert(body.command))
    except ValueError as error:
        raise ApiError(str(error), 422) from None
