"""Note endpoints.

Notes are private to their author: every route works on the caller's own notes only, and
nothing here (or anywhere) lets an administrator read someone else's.

Notes are deliberately not written to the audit log: their existence on an object is as
private as their content.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, Field

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.notes import repository

router = APIRouter(prefix="/notes", tags=["notes"])

Importance = Literal["info", "warning", "todo"]
MAX_BODY = 4000
TYPE_PATTERN = r"^[a-z_]{1,30}$"


class NoteIn(BaseModel):
    """A new note on an object of a node."""

    node_id: str = Field(max_length=64)
    object_type: str = Field(pattern=TYPE_PATTERN)
    object_id: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=MAX_BODY)
    importance: Importance = "info"
    remind_at: AwareDatetime | None = None


class NotePatch(BaseModel):
    """Changes to a note; omitted fields stay as they are."""

    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY)
    importance: Importance | None = None
    pinned: bool | None = None
    done: bool | None = None
    remind_at: AwareDatetime | None = None
    clear_reminder: bool = False
    # The `updated_at` the editor was based on: a note changed elsewhere meanwhile is refused.
    expected_updated_at: str | None = Field(default=None, max_length=40)


def _settings(request: Request):
    return request.app.state.settings


@router.get("", summary="List your notes")
def list_notes(
    request: Request,
    node_id: str | None = Query(default=None, max_length=64),
    object_type: str | None = Query(default=None, pattern=TYPE_PATTERN),
    object_id: str | None = Query(default=None, max_length=500),
    done: bool | None = None,
    q: str | None = Query(default=None, max_length=100),
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the caller's own notes, with an optional text search."""
    notes = repository.list_notes(
        conn,
        _settings(request),
        who.id,
        node_id=node_id,
        object_type=object_type,
        object_id=object_id,
        done=done,
    )
    if q:
        needle = q.lower()
        notes = [
            n for n in notes if needle in n["body"].lower() or needle in n["object_id"].lower()
        ]
    return notes


@router.post("", status_code=201, summary="Write a note")
def create_note(
    body: NoteIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Attach a note to an object. Only on nodes the caller can see (otherwise 404)."""
    who.require("notes.write", registry.rights())
    visible = nodes_repo.get_node(conn, body.node_id) is not None and who.has(
        "nodes.read", registry.rights(), f"node:{body.node_id}"
    )
    if not visible:
        raise NotFound()
    if repository.count(conn, who.id) >= repository.MAX_PER_OWNER:
        raise ApiError("notes.too_many", 409)
    return repository.create(
        conn,
        _settings(request),
        who.id,
        body.node_id,
        body.object_type,
        body.object_id,
        body.body,
        body.importance,
        body.remind_at.isoformat() if body.remind_at else None,
    )


@router.patch("/{note_id}", summary="Change a note")
def update_note(
    note_id: str,
    body: NotePatch,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Change one of the caller's notes; 409 if it changed since the editor loaded it."""
    who.require("notes.write", registry.rights())
    settings = _settings(request)
    current = repository.get(conn, settings, who.id, note_id)
    if current is None:
        raise NotFound()
    if body.expected_updated_at and body.expected_updated_at != current["updated_at"]:
        raise ApiError("notes.conflict", 409)
    changes = body.model_dump(exclude_none=True, exclude={"clear_reminder", "expected_updated_at"})
    if "remind_at" in changes:
        changes["remind_at"] = changes["remind_at"].isoformat()
    if body.clear_reminder:
        changes["remind_at"] = None
    repository.update(conn, settings, who.id, note_id, changes)
    return repository.get(conn, settings, who.id, note_id)  # type: ignore[return-value]


@router.delete("/{note_id}", status_code=204, summary="Delete a note")
def delete_note(
    note_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete one of the caller's notes."""
    who.require("notes.write", registry.rights())
    if not repository.delete(conn, who.id, note_id):
        raise NotFound()
