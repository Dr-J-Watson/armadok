"""Deciding who is told about an event, and telling them.

Called through the `notify` event so any module can raise a notification without knowing how
or to whom it is delivered:

    await emit(app, "notify", event="worker.connected", node_id=..., params={...})

A person is considered only if they hold the event's right on the node: nobody is notified
about an object they cannot see. What they actually receive follows their own rules.

The inbox is written at once; deliveries to external channels run in the background, a few at a
time, each with a deadline. The caller (often a worker's connection) never waits for them, and a
slow or hostile receiver cannot hold anything up.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Any

from armadok.core import crypto
from armadok.core.errors import ApiError
from armadok.core.outbound import OutboundError
from armadok.modules.notifications import delivery, platform, quiet, repository
from armadok.modules.users import repository as users_repo

log = logging.getLogger("armadok.notifications")

MAX_CONCURRENT_DELIVERIES = 8
MAX_PENDING_DELIVERIES = 500  # beyond this, extra deliveries are dropped (the inbox still works)


def _state(app: Any) -> dict[str, Any]:
    """Per-application bookkeeping: running deliveries, the concurrency limit, cooldowns."""
    if not hasattr(app.state, "notify_state"):
        app.state.notify_state = {
            "tasks": set(),
            "slots": asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES),
            "last": {},
        }
    return app.state.notify_state


def _eligible(conn, registry, event, node_id: str | None, only: list[str] | None) -> list[str]:
    """Return the ids of active accounts allowed to be told about this event."""
    catalogue = registry.rights()
    scope = f"node:{node_id}" if node_id else "*"
    right = event.right or ("nodes.read" if node_id else None)
    # Service accounts have no inbox: only people are told.
    users = [
        u["id"] for u in users_repo.list_users(conn) if u["is_active"] and u["kind"] == "person"
    ]
    if only is not None:
        users = [u for u in users if u in only]
    if right is None:
        return users
    # A suspended node must not mute its owner's notifications (an emergency access to it is
    # exactly what they need to hear about), and a background check is not a session: an open
    # emergency session of an administrator does not count here.
    return [
        u
        for u in users
        if registry.principal_for(conn, u, kind="background").has(
            right, catalogue, scope, ignore_blocked=True
        )
    ]


def _targets(
    rules: list[dict[str, Any]], event: str, severity: str, node_id: str | None
) -> set[str]:
    """Return where this account wants this event: "in_app" and/or channel ids.

    An account with no rule for the event gets the default: its inbox.
    """
    mine = [r for r in rules if r["event"] == event]
    if not mine:
        return {"in_app"}
    return {
        r["target"]
        for r in mine
        if r["enabled"]
        and repository.severity_rank(severity) >= repository.severity_rank(r["min_severity"])
        and (r["node_id"] is None or r["node_id"] == node_id)
    }


def _plan(
    app: Any, definition: Any, event: str, node_id: str | None, params: dict, user_ids: list | None
) -> list[tuple[str, dict[str, Any], str, str]]:
    """Write the inboxes and return the external deliveries to make (runs in a thread).

    Each delivery is `(channel_id, config, title, body)`. A problem with one person never stops
    the others.
    """
    settings = app.state.settings
    conn = settings.open_db()
    deliveries: list[tuple[str, dict[str, Any], str, str]] = []
    try:
        mail_allowed = platform.email_enabled(conn)
        for user_id in _eligible(conn, app.state.registry, definition, node_id, user_ids):
            try:
                targets = _targets(
                    repository.list_rules(conn, user_id), event, definition.severity, node_id
                )
                if definition.severity != "critical" and quiet.is_quiet(quiet.get(conn, user_id)):
                    targets = {"in_app"}  # quiet hours: the inbox always keeps it, nothing else
                if "in_app" in targets:
                    repository.add_to_inbox(
                        conn, user_id, event, definition.severity, node_id, params
                    )
                language = users_repo.get_language(conn, user_id)
                for target in targets - {"in_app"}:
                    row = repository.get_channel(conn, user_id, target)
                    # A mailbox nobody has confirmed is never written to.
                    if row is not None and row["confirmed"]:
                        config = json.loads(crypto.decrypt(settings, row["config"]))
                        if config["kind"] == "email" and not mail_allowed:
                            continue  # an administrator turned the e-mail channel off
                        # Free text is neutralised for this kind of channel (mentions, Markdown).
                        title, body = delivery.render(definition, params, language, config["kind"])
                        deliveries.append((row["id"], config, title, body))
            except Exception:  # noqa: BLE001
                log.exception("could not prepare a notification for one account")
    finally:
        conn.close()
    return deliveries


def _record_status(app: Any, channel_id: str, status: str) -> None:
    conn = app.state.settings.open_db()
    try:
        repository.set_status(conn, channel_id, status)
    finally:
        conn.close()


# Messages one channel may send per hour: a mailbox cannot be flooded through a rule.
_HOURLY_LIMIT = {"email": 20}
_DEFAULT_HOURLY_LIMIT = 120


def _within_rate(state: dict, channel_id: str, kind: str) -> bool:
    """Count a delivery against the channel's hourly budget; False when it is spent."""
    now = time.monotonic()
    sent = state.setdefault("sent", {}).setdefault(channel_id, collections.deque())
    while sent and now - sent[0] > 3600:
        sent.popleft()
    if len(sent) >= _HOURLY_LIMIT.get(kind, _DEFAULT_HOURLY_LIMIT):
        return False
    sent.append(now)
    return True


async def _deliver(
    app: Any,
    channel_id: str,
    config: dict,
    event: str,
    severity: str,
    title: str,
    body: str,
    node_id: str | None,
) -> None:
    """Send one message through one channel, within the concurrency limit; remember the result."""
    state = _state(app)
    if not _within_rate(state, channel_id, config.get("kind", "")):
        await asyncio.to_thread(_record_status, app, channel_id, "notifications.rate_limited")
        return
    async with state["slots"]:
        try:
            await delivery.send(
                config,
                event=event,
                severity=severity,
                title=title,
                body=body,
                node_id=node_id,
                allow_private=app.state.settings.allow_private_outbound,
            )
            status = "ok"
        except (OutboundError, ApiError) as error:
            # Only a generic status is kept: the exact reason would let an account learn what is
            # behind an address it cannot reach itself (an internal name, an open port).
            status = "outbound.failed" if isinstance(error, OutboundError) else error.code
            log.warning("notification channel %s failed: %s", channel_id, error.code)
        except Exception:  # noqa: BLE001
            status = "notifications.delivery_failed"
            log.exception("notification channel %s failed", channel_id)
    await asyncio.to_thread(_record_status, app, channel_id, status)


async def wait_for_deliveries(app: Any) -> None:
    """Wait until the deliveries started so far are finished (used by tests and on shutdown)."""
    tasks = list(_state(app)["tasks"])
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def handle_notify(
    app: Any,
    event: str,
    node_id: str | None = None,
    params: dict[str, Any] | None = None,
    user_ids: list[str] | None = None,
) -> None:
    """Raise a notification. Failures to deliver are logged and never break the caller."""
    definition = app.state.registry.notification_events().get(event)
    if definition is None:
        log.error("unknown notification event %s", event)
        return
    state = _state(app)
    if definition.cooldown:
        key, now = (event, node_id), time.monotonic()
        if now - state["last"].get(key, -1e9) < definition.cooldown:
            return  # the same thing just happened: do not repeat it
        state["last"][key] = now
    planned = await asyncio.to_thread(
        _plan, app, definition, event, node_id, params or {}, user_ids
    )
    for channel_id, config, title, body in planned:
        if len(state["tasks"]) >= MAX_PENDING_DELIVERIES:
            log.warning("too many pending deliveries: dropping one")
            break
        task = asyncio.create_task(
            _deliver(app, channel_id, config, event, definition.severity, title, body, node_id)
        )
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)
