"""Endpoints of project templates: a person's own Compose texts to start new projects from.

Private to the account and reachable with a person's own session only (a template may hold a
value somebody pasted inline, so a leaked key must not read them).
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


@router.get("", summary="List your templates")
def list_templates(
    conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)
) -> list[dict]:
    """Return your templates without their text."""
    return repository.list_for(conn, who.id)


@router.get("/{template_id}", summary="Read one of your templates")
def read_template(
    template_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return a template with its Compose text; someone else's is simply not found."""
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
