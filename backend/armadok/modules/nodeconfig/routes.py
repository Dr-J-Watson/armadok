"""Endpoints of a node's settings: read what is in force, and change it.

Reading needs `nodeconfig.read`. Changing needs `nodeconfig.write`, which is sensitive (never
for an API key) and, for an account with a second factor, a fresh code: a place decides what the
worker may touch. A place can only be narrowed inside the root its worker's environment gives, so
a value is first tested by the worker itself and refused if it leads out or does not exist.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodeconfig import catalogue, repository
from armadok.modules.users import second_factor
from armadok.modules.workers.access import audit_failure, authorise
from armadok.modules.workers.routes import call_operation

router = APIRouter(prefix="/nodes/{node_id}/settings", tags=["nodeconfig"])


class Place(BaseModel):
    """What the worker says about one place (untrusted)."""

    model_config = ConfigDict(extra="ignore", strict=True)
    kind: Literal["dir", "file"]
    root: str | None = Field(default=None, max_length=500)
    value: str = Field(max_length=400)
    valid: bool


class Described(BaseModel):
    """The worker's answer to `config.describe`."""

    model_config = ConfigDict(extra="ignore", strict=True)
    settings: dict[str, Place] = Field(max_length=20)


class Checked(BaseModel):
    """The worker's answer to `config.check`."""

    model_config = ConfigDict(extra="ignore", strict=True)
    errors: dict[str, Literal["invalid", "outside", "not_found", "not_configured", "forbidden"]] = (
        Field(max_length=20)
    )


class ChangeIn(BaseModel):
    """Changes to a node's settings; an omitted setting is left as it is, "" resets it."""

    model_config = ConfigDict(extra="forbid")
    places: dict[str, str] = Field(default_factory=dict, max_length=10)
    traefik_entrypoints: list[str] | None = Field(
        default=None, max_length=catalogue.MAX_ENTRYPOINTS
    )
    traefik_resolver: str | None = Field(default=None, max_length=64)
    code: str = Field(default="", max_length=32)

    @field_validator("places")
    @classmethod
    def known_places(cls, value: dict[str, str]) -> dict[str, str]:
        """Only known places, with values of a sensible size."""
        for name, text in value.items():
            if name not in catalogue.PLACES or len(text) > 400:
                raise ValueError("unknown place")
        return value

    @field_validator("traefik_entrypoints")
    @classmethod
    def plain_names(cls, value: list[str] | None) -> list[str] | None:
        """Entry point names are plain names."""
        if value is not None and any(not catalogue.NAME.fullmatch(n) for n in value):
            raise ValueError("not a name")
        return value

    @field_validator("traefik_resolver")
    @classmethod
    def plain_resolver(cls, value: str | None) -> str | None:
        """Require a plain name, or empty to reset."""
        if value and not catalogue.NAME.fullmatch(value):
            raise ValueError("not a name")
        return value


def _node_type(conn: sqlite3.Connection, node_id: str) -> str:
    row = conn.execute("SELECT type FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row["type"] if row else "docker"


async def _ask(request: Request, registry: Registry, node_id: str, op: str, args: dict, model):
    """Run an operation on the node's worker and validate the answer."""
    result = await call_operation(request.app, registry, node_id, op, args)
    try:
        return model.model_validate(result)
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None


@router.get("", summary="Settings of a node")
async def read_settings(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Return the places this node can move (with their roots) and the Traefik defaults.

    If the worker cannot be asked, `error` holds the code and the saved values are returned.
    """
    authorise(conn, registry, who, node_id, "nodeconfig.read")
    kept = repository.all_for(conn, node_id)
    relevant = catalogue.PLACES_BY_TYPE.get(_node_type(conn, node_id), ())
    places: dict[str, Any] = {
        name: {"root": None, "value": kept.get(name, ""), "kind": None, "valid": True}
        for name in relevant
    }
    error = None
    try:
        described = await _ask(request, registry, node_id, "config.describe", {}, Described)
        for name in relevant:
            if name in described.settings:
                places[name] = described.settings[name].model_dump()
    except ApiError as failure:
        error = failure.code
    can_write = who.kind == "session" and who.has(
        "nodeconfig.write", registry.rights(), f"node:{node_id}"
    )
    if not can_write:
        # The layout of the node is for those who may change it: others see only the last name.
        for place in places.values():
            if place.get("root"):
                place["root"] = "\u2026/" + place["root"].rstrip("/").rsplit("/", 1)[-1]
    return {
        "places": places,
        "traefik_entrypoints": catalogue.entrypoints_of(kept.get(catalogue.ENTRYPOINTS, "")),
        "traefik_resolver": kept.get(catalogue.RESOLVER, ""),
        "can_write": can_write,
        "error": error,
    }


def _fresh_code(
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    node_id: str,
    code: str,
) -> None:
    """Demand a fresh second-factor code when the account has one, from the person only."""
    if who.kind != "session":
        raise MissingRight("nodeconfig.not_with_key")
    if not second_factor.is_enabled(conn, who.id):
        return
    try:
        second_factor.require_fresh_code(conn, request.app.state.settings, who.id, code)
    except ApiError as error:
        if error.code != "auth.too_many_attempts":
            audit.record(
                conn,
                registry.audit_events(),
                who.actor,
                "nodeconfig.update",
                object_type="node",
                object_id=node_id,
                outcome="denied",
                details={"code": error.code},
            )
        raise


@router.put("", summary="Change the settings of a node")
async def change_settings(
    node_id: str,
    body: ChangeIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Keep the given settings. Places are tested by the worker first.

    Needs `nodeconfig.write` and, with a second factor on the account, a fresh code. The audit
    entry names the settings changed, with the new value of each place (a relative folder name,
    nothing secret).
    """
    authorise(conn, registry, who, node_id, "nodeconfig.write")
    allowed = catalogue.PLACES_BY_TYPE.get(_node_type(conn, node_id), ())
    if any(name not in allowed for name in body.places):
        raise ApiError("nodeconfig.not_for_node_type", 422)
    _fresh_code(request, conn, registry, who, node_id, body.code)
    changes: dict[str, str] = dict(body.places)
    if body.traefik_entrypoints is not None:
        changes[catalogue.ENTRYPOINTS] = ",".join(body.traefik_entrypoints)
    if body.traefik_resolver is not None:
        changes[catalogue.RESOLVER] = body.traefik_resolver
    details = {"changed": ",".join(sorted(changes))[:200]}
    if body.places:
        try:
            checked = await _ask(
                request, registry, node_id, "config.check", {"values": body.places}, Checked
            )
        except ApiError as error:
            audit_failure(conn, registry, who, "nodeconfig.update", node_id, details, error)
            raise
        if checked.errors:
            audit_failure(
                conn,
                registry,
                who,
                "nodeconfig.update",
                node_id,
                {**details, "refused": ",".join(sorted(checked.errors))[:200]},
                ApiError("nodeconfig.invalid_place", 422),
            )
            raise ApiError("nodeconfig.invalid_place", 422)
    repository.save(conn, node_id, changes, who.actor)
    conn.commit()
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "nodeconfig.update",
        object_type="node",
        object_id=node_id,
        details={**details, **{k: v for k, v in body.places.items() if v}},
    )
    return {"changed": sorted(changes)}


class CheckIn(BaseModel):
    """Places to test without keeping them."""

    model_config = ConfigDict(extra="forbid")
    places: dict[str, str] = Field(max_length=10)

    @field_validator("places")
    @classmethod
    def known_places(cls, value: dict[str, str]) -> dict[str, str]:
        """Only known places, with values of a sensible size."""
        for name, text in value.items():
            if name not in catalogue.PLACES or len(text) > 400:
                raise ValueError("unknown place")
        return value


@router.post("/check", summary="Test places without keeping them")
async def check_places(
    node_id: str,
    body: CheckIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Say, for each place, why the worker would refuse it (an empty answer means all is well).

    Needs `nodeconfig.write`: it tells which folders exist under a root.
    """
    authorise(conn, registry, who, node_id, "nodeconfig.write")
    allowed = catalogue.PLACES_BY_TYPE.get(_node_type(conn, node_id), ())
    if any(name not in allowed for name in body.places):
        raise ApiError("nodeconfig.not_for_node_type", 422)
    checked = await _ask(
        request, registry, node_id, "config.check", {"values": body.places}, Checked
    )
    return {"errors": checked.errors}


class Entry(BaseModel):
    """One name offered by the worker (untrusted): a plain name only."""

    model_config = ConfigDict(extra="ignore", strict=True)
    name: str = Field(max_length=64, pattern=catalogue.NAME.pattern)
    project: bool


class Browsed(BaseModel):
    """The worker's answer to `config.browse`."""

    model_config = ConfigDict(extra="ignore", strict=True)
    kind: Literal["dir", "file"]
    entries: list[Entry] = Field(max_length=200)
    truncated: bool = False


@router.get("/browse", summary="List what can be picked for a place")
async def browse(
    node_id: str,
    place: str,
    request: Request,
    path: str = "",
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Return the sub-folders (or sibling files) of a place under its root, one level.

    Needs `nodeconfig.write`: it tells which folders exist. A project folder is marked, not
    hidden, so the interface can show why it cannot be chosen.
    """
    authorise(conn, registry, who, node_id, "nodeconfig.write")
    if place not in catalogue.PLACES_BY_TYPE.get(_node_type(conn, node_id), ()) or len(path) > 400:
        raise ApiError("nodeconfig.not_for_node_type", 422)
    answer = await _ask(
        request, registry, node_id, "config.browse", {"place": place, "path": path}, Browsed
    )
    return answer.model_dump()
