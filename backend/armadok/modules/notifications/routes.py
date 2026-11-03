"""Notification endpoints: inbox, channels and rules.

Everything is about the caller's own account: nobody reads another person's inbox, channels or
rules.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from armadok.core import crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.outbound import OutboundError, check_url
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.notifications import delivery, repository
from armadok.modules.users import repository as users_repo

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ChannelIn(BaseModel):
    """A channel to add: its kind and the settings that kind needs."""

    kind: str = Field(pattern="^(webhook|ntfy|discord)$")
    name: str = Field(min_length=1, max_length=60)
    settings: dict[str, Any] = Field(default_factory=dict, max_length=10)


class RuleIn(BaseModel):
    """One rule: send `event` to `target` ("in_app" or a channel id) from `min_severity` up."""

    event: str = Field(max_length=60)
    target: str = Field(max_length=64)
    min_severity: str = Field(default="info", pattern="^(info|warning|critical)$")
    node_id: str | None = Field(default=None, max_length=64)
    enabled: bool = True


class RulesIn(BaseModel):
    """The full set of an account's rules (replaces what was there)."""

    rules: list[RuleIn] = Field(max_length=repository.MAX_RULES)


class ReadIn(BaseModel):
    """Entries to mark as read; `null` means all."""

    ids: list[int] | None = Field(default=None, max_length=1000)


def _language(conn: sqlite3.Connection, who: Principal) -> str:
    return users_repo.get_language(conn, who.id)


@router.get("", summary="Your notifications")
def inbox(
    unread: bool = False,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the caller's inbox, newest first, written in their language."""
    language = _language(conn, who)
    catalogue = registry.notification_events()
    entries = []
    for row in repository.list_inbox(conn, who.id, unread, before_id, limit):
        definition = catalogue.get(row["event"])
        title, body = (
            delivery.render(definition, row["params"], language)
            if definition
            else (row["event"], "")
        )
        entries.append({**row, "title": title, "body": body, "params": None})
    return {
        "entries": entries,
        "next_before_id": entries[-1]["id"] if len(entries) == limit else None,
        "unread": repository.unread_count(conn, who.id),
    }


@router.get("/unread-count", summary="How many unread notifications")
def unread(
    conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)
) -> dict:
    """Return the number shown on the bell."""
    return {"unread": repository.unread_count(conn, who.id)}


@router.post("/read", status_code=204, summary="Mark notifications as read")
def read(
    body: ReadIn,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> None:
    """Mark some, or all, of the caller's notifications as read."""
    repository.mark_read(conn, who.id, body.ids)


@router.get("/events", summary="The kinds of notification that exist")
def events(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return every event with its usual severity and a title in the caller's language."""
    language = _language(conn, who)
    return [
        {"name": e.name, "severity": e.severity, "title": delivery.render(e, {}, language)[0]}
        for e in sorted(registry.notification_events().values(), key=lambda e: e.name)
    ]


# --- channels -----------------------------------------------------------------------------


@router.get("/channels", summary="Your channels")
def channels(conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)):
    """Return the caller's channels. Their settings (URLs, tokens) are never sent back."""
    return repository.list_channels(conn, who.id)


@router.post("/channels", status_code=201, summary="Add a channel")
def add_channel(
    body: ChannelIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> dict:
    """Add a channel. Its settings are encrypted; a webhook's signing secret is shown only now."""
    if len(repository.list_channels(conn, who.id)) >= repository.MAX_CHANNELS:
        raise ApiError("notifications.too_many_channels", 409)
    model = delivery.CONFIGS[body.kind]
    try:
        config = model.model_validate({**body.settings, "kind": body.kind})
    except ValueError:
        raise ApiError("notifications.invalid_channel", 422) from None
    data = config.model_dump()
    url = data.get("url") or data.get("server")
    try:
        if url:
            check_url(url)
    except OutboundError as error:
        raise ApiError(error.code, 422) from None
    shown_secret = None
    if body.kind == "webhook":
        shown_secret = data["secret"] = secrets.token_urlsafe(32)
    sealed = crypto.encrypt(request.app.state.settings, json.dumps(data))
    channel_id = repository.create_channel(conn, who.id, body.kind, body.name, sealed)
    return {"id": channel_id, "secret": shown_secret}


@router.delete("/channels/{channel_id}", status_code=204, summary="Remove a channel")
def remove_channel(
    channel_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove one of the caller's channels (and the rules that used it)."""
    if not repository.delete_channel(conn, who.id, channel_id):
        raise NotFound()


_TEST_COOLDOWN_SECONDS = 10


@router.post("/channels/{channel_id}/test", summary="Send a test message")
async def test_channel(
    channel_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> dict:
    """Send a harmless test message through one of the caller's channels.

    At most one test every few seconds per account: this makes the hub call an address the
    caller chose, so it must not be a way to hammer someone else's server.
    """
    row = repository.get_channel(conn, who.id, channel_id)
    if row is None:
        raise NotFound()
    recent = request.app.state.__dict__.setdefault("notify_tests", {})
    now = time.monotonic()
    if now - recent.get(who.id, -1e9) < _TEST_COOLDOWN_SECONDS:
        raise ApiError("notifications.too_fast", 429)
    recent[who.id] = now
    settings = request.app.state.settings
    language = _language(conn, who)
    title, body = (
        ("Armadok test", "This is a test notification.")
        if language == "en"
        else ("Test Armadok", "Ceci est une notification de test.")
    )
    try:
        await delivery.send(
            json.loads(crypto.decrypt(settings, row["config"])),
            event="test",
            severity="info",
            title=title,
            body=body,
            node_id=None,
            allow_private=settings.allow_private_outbound,
        )
    except OutboundError as error:
        repository.set_status(conn, channel_id, error.code)
        # Only a generic code goes back: the receiver's exact answer would let a user probe
        # internal services when private addresses are allowed.
        raise ApiError("outbound.failed", 502) from None
    except Exception:  # noqa: BLE001
        repository.set_status(conn, channel_id, "notifications.delivery_failed")
        raise ApiError("notifications.delivery_failed", 502) from None
    repository.set_status(conn, channel_id, "ok")
    return {"status": "ok"}


# --- rules --------------------------------------------------------------------------------


@router.get("/rules", summary="Your rules")
def rules(conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)):
    """Return the caller's rules (an event with no rule goes to the inbox)."""
    return repository.list_rules(conn, who.id)


@router.put("/rules", summary="Replace your rules")
def set_rules(
    body: RulesIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Replace all the caller's rules. Events, channels and nodes must exist and be theirs."""
    known = registry.notification_events()
    mine = {c["id"] for c in repository.list_channels(conn, who.id)}
    for rule in body.rules:
        if rule.event not in known or rule.target not in {"in_app", *mine}:
            raise ApiError("notifications.invalid_rule", 422)
        if rule.node_id is not None and not (
            nodes_repo.get_node(conn, rule.node_id) is not None
            and who.has("nodes.read", registry.rights(), f"node:{rule.node_id}")
        ):
            raise ApiError("notifications.invalid_rule", 422)
    repository.replace_rules(conn, who.id, [r.model_dump() for r in body.rules])
    return repository.list_rules(conn, who.id)
