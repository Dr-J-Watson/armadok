"""Note endpoints.

Notes are private to their author: every route works on the caller's own notes only, and
nothing here (or anywhere) lets an administrator read someone else's. The one exception is the
author's own choice: a note can be shown, read-only, to named people who can see its node.

Notes are deliberately not written to the audit log: their existence on an object is as
private as their content.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, Field

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.core.throttle import FailureLimiter
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.notes import repository
from armadok.modules.users import repository as users_repo


def _own_session_only(who: Principal = Depends(get_principal)) -> None:
    """Allow notes to be reached with a person's own session only, never with an API key.

    A key is made to be handed to a tool, and a leaked one must not be a way to read a person's
    private words. (Search already refuses keys for the same reason.)
    """
    if who.kind != "session":
        raise MissingRight("notes.not_with_key")


router = APIRouter(prefix="/notes", tags=["notes"], dependencies=[Depends(_own_session_only)])

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
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the caller's own notes (the text search is done in the browser, not in the URL)."""
    notes = repository.list_notes(
        conn,
        _settings(request),
        who.id,
        node_id=node_id,
        object_type=object_type,
        object_id=object_id,
        done=done,
    )
    return notes


class ShareIn(BaseModel):
    """The person to show a note to."""

    user_id: str = Field(min_length=1, max_length=64)


@router.get("/shared", summary="Notes others shared with you")
def shared_notes(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return notes shared with you, read-only, on nodes you can still see."""
    visible = [
        n
        for n in repository.shared_to(conn, _settings(request), who.id)
        if nodes_repo.get_node(conn, n["node_id"]) is not None
        and who.has("nodes.read", registry.rights(), f"node:{n['node_id']}")
    ]
    return visible


people_limiter = FailureLimiter({"user": 10})


def _may_see(conn, registry, user_id: str, node_id: str) -> bool:
    """Tell whether an active, unblocked person can see the node."""
    if registry.account_blocked(conn, user_id):
        return False
    return registry.principal_for(conn, user_id, kind="background").has(
        "nodes.read", registry.rights(), f"node:{node_id}"
    )


@router.get("/people", summary="Find the person to show a note to")
def share_candidate(
    node_id: str = Query(max_length=64),
    username: str = Query(min_length=1, max_length=64),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the person with exactly this user name, if they can see the node.

    An exact name, not a prefix, and a few tries a minute: this must not be a way to list who
    has access to a node. Nothing is returned for someone who cannot see it.
    """
    if nodes_repo.get_node(conn, node_id) is None or not who.has(
        "nodes.read", registry.rights(), f"node:{node_id}"
    ):
        raise NotFound()
    if people_limiter.blocked(user=who.id):
        raise ApiError("members.too_many_searches", 429)
    people_limiter.remember(user=who.id)
    row = conn.execute(
        "SELECT id, username FROM users WHERE lower(username) = lower(?) AND is_active = 1"
        " AND kind = 'person' AND id != ?",
        (username, who.id),
    ).fetchone()
    if row is None or not _may_see(conn, registry, row["id"], node_id):
        return []
    return [{"id": row["id"], "username": row["username"]}]


@router.get("/{note_id}/shares", summary="Who a note is shared with")
def note_shares(
    note_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the people one of your notes is shared with (404 if it is not yours)."""
    if repository.get(conn, _settings(request), who.id, note_id) is None:
        raise NotFound()
    return [
        {"user_id": uid, "username": (users_repo.get_user(conn, uid) or {}).get("username", "?")}
        for uid in repository.shared_with(conn, note_id)
    ]


@router.post("/{note_id}/shares", status_code=201, summary="Show a note to someone")
def share_note(
    note_id: str,
    body: ShareIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Share one of your notes, read-only, with a person who can see the node it is about.

    Only you can share or stop sharing it. Someone who loses access to the node stops seeing
    the note. A key cannot share (it is a decision about who reads your words).
    """
    who.require("notes.write", registry.rights())
    note = repository.get(conn, _settings(request), who.id, note_id)
    if note is None:
        raise NotFound()
    target = users_repo.get_user(conn, body.user_id)
    if target is None or not target["is_active"] or target["kind"] != "person":
        raise NotFound()
    if target["id"] == who.id:
        raise ApiError("notes.share_self", 409)
    if people_limiter.blocked(user=who.id):
        raise ApiError("members.too_many_searches", 429)
    people_limiter.remember(user=who.id)
    # Someone who could not see the node would learn that it exists: not shareable with them.
    if not _may_see(conn, registry, target["id"], note["node_id"]):
        raise ApiError("notes.share_no_access", 409)
    if repository.share(conn, who.id, note_id, target["id"]) == "full":
        raise ApiError("notes.too_many_shares", 409)
    return {"user_id": target["id"], "username": target["username"]}


@router.delete("/{note_id}/shares/{user_id}", status_code=204, summary="Stop showing a note")
def unshare_note(
    note_id: str,
    user_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> None:
    """Stop sharing one of your notes with a person."""
    if not repository.unshare(conn, who.id, note_id, user_id):
        raise NotFound()


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
