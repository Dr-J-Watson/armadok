"""Endpoint of the converter: paste a `docker run` command, get a Compose file."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.convert import containers, dockerrun
from armadok.modules.workers.access import ask, audit_failure, authorise

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


node_router = APIRouter(prefix="/nodes/{node_id}/convert", tags=["convert"])


class ContainersIn(BaseModel):
    """The containers to convert, by id."""

    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12,64}$")]] = Field(
        min_length=1, max_length=20, description="Container ids (hex)"
    )


@node_router.post("/containers", summary="Turn running containers into a Compose file")
async def from_containers(
    node_id: str,
    body: ContainersIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Read how the containers were started and return a Compose file and a `.env` text.

    Needs `convert.containers` (sensitive: the environment variables are read). Nothing is
    stored or changed on the node. The audit entry names the node and how many containers,
    never the settings or the variables.
    """
    authorise(conn, registry, who, node_id, "convert.containers")
    details = {"count": len(body.ids)}
    try:
        answer = await ask(
            request,
            registry,
            node_id,
            "docker.containers.inspect",
            {"ids": body.ids},
            containers.Answer,
        )
        if len(answer.containers) != len(body.ids):
            raise ApiError("worker.invalid_answer", 502)
        result = dict(containers.convert(answer.containers))
    except ApiError as error:
        audit_failure(conn, registry, who, "convert.containers", node_id, details, error)
        raise
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "convert.containers",
        object_type="node",
        object_id=node_id,
        details=details,
    )
    return result
