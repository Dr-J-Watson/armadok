"""Endpoints of project templates: a person's own Compose texts to start new projects from.

Private to the account unless its owner shares it, and reachable with a person's own session
only (a template may hold a value somebody pasted inline, so a leaked key must not read them).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.templates import repository

MAX_COMPOSE = 128 * 1024


def _own_session_only(who: Principal = Depends(get_principal)) -> None:
    """Refuse API keys: templates belong to the person."""
    if who.kind != "session":
        raise MissingRight("templates.not_with_key")


router = APIRouter(
    prefix="/templates", tags=["templates"], dependencies=[Depends(_own_session_only)]
)


class TemplateIn(BaseModel):
    """A template to keep."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    compose: str = Field(min_length=1, max_length=MAX_COMPOSE)


class SharingIn(BaseModel):
    """Who a template is shared with: everybody on this Armadok, and/or some of your groups."""

    model_config = ConfigDict(extra="forbid")
    everyone: bool = False
    groups: list[str] = Field(default_factory=list, max_length=repository.MAX_SHARED_GROUPS)


@router.get("/groups", summary="Your groups (the ones you may share with)")
def my_groups(
    conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)
) -> list[dict]:
    """Return the groups you belong to."""
    return repository.my_groups(conn, who.id)


@router.put("/{template_id}/sharing", status_code=204, summary="Share a template")
def share_template(
    template_id: str,
    body: SharingIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Let everybody, or the members of some of your groups, start projects from your template.

    They can use it, not change or delete it. You may only name groups you belong to.
    """
    allowed = {g["id"] for g in repository.my_groups(conn, who.id)}
    if not set(body.groups) <= allowed:
        raise ApiError("templates.not_your_group", 422)
    if not repository.set_sharing(conn, who.id, template_id, body.everyone, body.groups):
        raise ApiError("templates.not_found", 404)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "templates.share",
        object_type="template",
        object_id=template_id,
        details={"everyone": body.everyone, "groups": len(body.groups)},
    )


@router.get("", summary="List your templates")
def list_templates(
    conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)
) -> list[dict]:
    """Return the templates you may use (yours and shared ones), without their text."""
    return repository.list_for(conn, who.id)


@router.get("/{template_id}", summary="Read one of your templates")
def read_template(
    template_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return a usable template with its Compose text; any other is simply not found."""
    found = repository.get(conn, request.app.state.settings, who.id, template_id)
    if found is None:
        raise ApiError("templates.not_found", 404)
    return found


@router.post("", status_code=201, summary="Keep a template")
def create_template(
    body: TemplateIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Store a Compose text under a name. The audit entry names it, never the text."""
    if repository.count(conn, who.id) >= repository.MAX_PER_OWNER:
        raise ApiError("templates.too_many", 409)
    made = repository.create(
        conn, request.app.state.settings, who.id, body.name, body.description, body.compose
    )
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "templates.create",
        object_type="template",
        object_id=made["id"],
        details={"name": body.name},
    )
    return made


@router.delete("/{template_id}", status_code=204, summary="Delete a template")
def delete_template(
    template_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete one of your templates."""
    if not repository.delete(conn, who.id, template_id):
        raise ApiError("templates.not_found", 404)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "templates.delete",
        object_type="template",
        object_id=template_id,
    )
