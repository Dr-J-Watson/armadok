"""Notification endpoints: inbox, channels and rules.

Everything is about the caller's own account: nobody reads another person's inbox, channels or
rules.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit, crypto, webpush
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.outbound import OutboundError, check_mail_port, check_url
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.notifications import delivery, platform, push_keys, quiet, repository
from armadok.modules.users import repository as users_repo
from armadok.modules.users.security import token_hash

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _audit(conn, registry, who, event, **details) -> None:
    """Record a change of channel or quiet hours (kind and name only: never the settings)."""
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="notification_channel",
        details=details,
    )


MAX_PER_DESTINATION = 5  # channels of one account aimed at the same host


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _check_destination_cap(conn, settings, owner_id: str, url: str) -> None:
    """Refuse a sixth channel aimed at one host: an account could otherwise multiply its sends."""
    host, same = _host_of(url), 0
    for channel in repository.list_channels(conn, owner_id):
        row = repository.get_channel(conn, owner_id, channel["id"])
        try:
            config = json.loads(crypto.decrypt(settings, row["config"]))
        except (ApiError, ValueError):
            continue
        other = config.get("url") or config.get("server") or config.get("endpoint") or ""
        same += _host_of(other) == host
    if same >= MAX_PER_DESTINATION:
        raise ApiError("notifications.too_many_channels", 409)


class ChannelIn(BaseModel):
    """A channel to add: its kind and the settings that kind needs."""

    kind: str = Field(pattern="^(webhook|ntfy|discord|email|webpush)$")
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


@router.get("/push-key", summary="The key a browser needs to receive notifications")
async def push_key(request: Request, who: Principal = Depends(get_principal)) -> dict:
    """Return the server's public push key (made the first time it is asked for)."""
    key = await asyncio.to_thread(push_keys.load, request.app.state.settings)
    return {"public_key": webpush.vapid_public(key)}


@router.get("/channels", summary="Your channels")
def channels(conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)):
    """Return the caller's channels. Their settings (URLs, tokens) are never sent back."""
    return repository.list_channels(conn, who.id)


@router.post("/channels", status_code=201, summary="Add a channel")
async def add_channel(
    body: ChannelIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Add a channel. Its settings are encrypted; a webhook's signing secret is shown only now.

    A mailbox is not used until its owner enters the code sent to it: nobody can make the hub
    write to an address that is not theirs.
    """
    if len(repository.list_channels(conn, who.id)) >= repository.MAX_CHANNELS:
        raise ApiError("notifications.too_many_channels", 409)
    if body.kind == "email" and not platform.email_enabled(conn):
        raise ApiError("notifications.email_disabled", 409)
    model = delivery.CONFIGS[body.kind]
    try:
        config = model.model_validate({**body.settings, "kind": body.kind})
    except ValueError:
        raise ApiError("notifications.invalid_channel", 422) from None
    data = config.model_dump()
    url = data.get("url") or data.get("server") or data.get("endpoint")
    settings = request.app.state.settings
    try:
        if url:
            check_url(url)
        if body.kind == "email":
            check_mail_port(data["port"], settings.allow_private_outbound)
    except OutboundError as error:
        raise ApiError(error.code, 422) from None
    if url:
        _check_destination_cap(conn, settings, who.id, url)
    shown_secret = None
    if body.kind == "webhook":
        shown_secret = data["secret"] = secrets.token_urlsafe(32)
    sealed = crypto.encrypt(settings, json.dumps(data))
    code = f"{secrets.randbelow(10**6):06d}" if body.kind == "email" else None
    channel_id = repository.create_channel(
        conn, who.id, body.kind, body.name, sealed, token_hash(code) if code else None
    )
    if code:
        language = _language(conn, who)
        title, text = (
            ("Confirm this address", f"Your Armadok confirmation code is {code}.")
            if language == "en"
            else ("Confirmez cette adresse", f"Votre code de confirmation Armadok est {code}.")
        )
        try:
            await delivery.send(
                data,
                event="confirmation",
                severity="info",
                title=title,
                body=text,
                node_id=None,
                allow_private=settings.allow_private_outbound,
            )
        except OutboundError:
            repository.delete_channel(conn, who.id, channel_id)
            raise ApiError("outbound.failed", 502) from None
    _audit(conn, registry, who, "notification.channel_create", kind=body.kind, name=body.name)
    return {"id": channel_id, "secret": shown_secret, "needs_confirmation": code is not None}


class ConfirmIn(BaseModel):
    """The code that was sent to the mailbox."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[0-9]{6}$")


@router.post("/channels/{channel_id}/confirm", summary="Confirm a mailbox")
def confirm_channel(
    channel_id: str,
    body: ConfirmIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Confirm a mailbox with the code it received; after a few wrong tries the channel locks."""
    result = repository.try_confirm(conn, who.id, channel_id, token_hash(body.code))
    if result == "none":
        raise NotFound()
    if result != "ok":
        raise ApiError(
            {
                "wrong": "notifications.wrong_code",
                "expired": "notifications.code_expired",
                "locked": "notifications.code_locked",
            }[result],
            422 if result != "locked" else 429,
        )
    _audit(conn, registry, who, "notification.channel_confirm")
    return {"confirmed": True}


@router.delete("/channels/{channel_id}", status_code=204, summary="Remove a channel")
def remove_channel(
    channel_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove one of the caller's channels (and the rules that used it)."""
    if not repository.delete_channel(conn, who.id, channel_id):
        raise NotFound()
    _audit(conn, registry, who, "notification.channel_delete")


_TEST_COOLDOWN_SECONDS = 10


@router.post("/channels/{channel_id}/test", summary="Send a test message")
async def test_channel(
    channel_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Send a harmless test message through one of the caller's channels.

    At most one test every few seconds per account: this makes the hub call an address the
    caller chose, so it must not be a way to hammer someone else's server.
    """
    row = repository.get_channel(conn, who.id, channel_id)
    if row is None:
        raise NotFound()
    if not row["confirmed"]:
        raise ApiError("notifications.unconfirmed", 409)
    if row["kind"] == "email" and not platform.email_enabled(conn):
        raise ApiError("notifications.email_disabled", 409)
    recent = request.app.state.__dict__.setdefault("notify_tests", {})
    now = time.monotonic()
    if now - recent.get(who.id, -1e9) < _TEST_COOLDOWN_SECONDS:
        raise ApiError("notifications.too_fast", 429)
    for stale in [k for k, t in recent.items() if now - t > _TEST_COOLDOWN_SECONDS]:
        del recent[stale]
    recent[who.id] = now
    settings = request.app.state.settings
    language = _language(conn, who)
    title, body = (
        ("Armadok test", "This is a test notification.")
        if language == "en"
        else ("Test Armadok", "Ceci est une notification de test.")
    )
    config = json.loads(crypto.decrypt(settings, row["config"]))
    try:
        key = (
            await asyncio.to_thread(push_keys.load, settings)
            if config["kind"] == "webpush"
            else None
        )
        await delivery.send(
            config,
            event="test",
            severity="info",
            title=title,
            body=body,
            node_id=None,
            allow_private=settings.allow_private_outbound,
            push_key=key,
            push_contact=settings.push_contact,
        )
    except OutboundError as error:
        if error.code == "notifications.push_gone":
            repository.set_status(conn, channel_id, "notifications.push_gone")
            raise ApiError("notifications.push_gone", 410) from None
        repository.set_status(conn, channel_id, "outbound.failed")
        # Only a generic code goes back: the receiver's exact answer would let a user probe
        # internal services when private addresses are allowed.
        raise ApiError("outbound.failed", 502) from None
    except Exception:  # noqa: BLE001
        repository.set_status(conn, channel_id, "notifications.delivery_failed")
        raise ApiError("notifications.delivery_failed", 502) from None
    repository.set_status(conn, channel_id, "ok")
    _audit(conn, registry, who, "notification.channel_test")
    return {"status": "ok"}


# --- quiet hours --------------------------------------------------------------------------


class QuietIn(BaseModel):
    """Do-not-disturb hours: from `start` to `end` (may cross midnight) in a time zone."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)
    timezone: str = Field(max_length=64)


@router.get("/quiet", summary="Your do-not-disturb hours")
def get_quiet(conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)):
    """Return the caller's quiet hours."""
    return quiet.get(conn, who.id)


@router.put("/quiet", summary="Set your do-not-disturb hours")
def set_quiet(
    body: QuietIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
):
    """Set the quiet hours: outside channels stay silent then, except for critical events."""
    try:
        quiet.parse(body.start)
        quiet.parse(body.end)
        quiet.zone(body.timezone)
    except ValueError:
        raise ApiError("notifications.invalid_quiet", 422) from None
    quiet.put(conn, who.id, body.model_dump())
    _audit(conn, registry, who, "notification.quiet_set", enabled=body.enabled)
    return quiet.get(conn, who.id)


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


class PlatformIn(BaseModel):
    """The platform switches an administrator may change."""

    model_config = ConfigDict(extra="forbid")
    email_enabled: bool


@router.get("/platform", summary="Platform-wide notification switches")
def get_platform(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
):
    """Tell whether the e-mail channel is available (everybody needs to know, to hide it).

    `can_change` says whether the caller may switch it, so the screen shows it only to them.
    """
    return {
        "email_enabled": platform.email_enabled(conn),
        "can_change": who.has("notifications.admin", registry.rights()),
    }


@router.put("/platform", summary="Change the platform-wide notification switches")
def set_platform(
    body: PlatformIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
):
    """Turn the e-mail channel on or off for everybody. Needs `notifications.admin`."""
    who.require("notifications.admin", registry.rights())
    platform.set_email_enabled(conn, body.email_enabled)
    _audit(conn, registry, who, "notification.platform_set", email_enabled=body.email_enabled)
    return {"email_enabled": body.email_enabled}
