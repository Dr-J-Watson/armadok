"""The connected screens, and who gets which signal.

Each connection is one logged-in person. A signal about a machine is passed on only to people
who can see that machine *now* (their rights are read again at each delivery, so a right
taken away stops the signals at once). Signals are gathered for a moment and sent together,
so a burst of changes becomes one message per kind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from armadok.core.module import Signal
from armadok.core.rights import Principal

log = logging.getLogger("armadok.realtime")

MAX_CLIENTS = 500
MAX_PER_USER = 6
GRANTS_CACHE_SECONDS = 1.0
BATCH_SECONDS = 0.25
SEND_TIMEOUT_SECONDS = 3
MAX_PENDING = 200  # a client that cannot keep up is cut, not buffered without limit


@dataclass(eq=False)
class Client:
    """One open connection."""

    ws: WebSocket
    user_id: str
    pending: set[tuple[str, str | None]] = field(default_factory=set)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    overflow: bool = False


class RealtimeHub:
    """Registry of connections; modules reach it only through the `signal` event."""

    def __init__(self) -> None:
        """Start with nobody connected."""
        self.clients: set[Client] = set()
        # Connections that have not logged in yet, in total and per address.
        self.waiting_total = 0
        self.waiting: dict[str, int] = {}
        # Grants read a moment ago, shared by the connections of one person: a burst of
        # changes must not mean one database round trip per connection per change.
        self.grants_cache: dict[str, tuple[float, Principal]] = {}

    def admit(self, ws: WebSocket, user_id: str) -> Client | None:
        """Register a connection, or return None when there are too many."""
        if len(self.clients) >= MAX_CLIENTS:
            return None
        if sum(1 for c in self.clients if c.user_id == user_id) >= MAX_PER_USER:
            return None
        client = Client(ws, user_id)
        self.clients.add(client)
        return client

    def drop(self, client: Client) -> None:
        """Forget a connection."""
        self.clients.discard(client)

    def publish(self, signal: str, node_id: str | None, actor_id: str | None, shared: bool) -> None:
        """Queue a signal. One that names no machine and is not shared goes to its author only."""
        for client in self.clients:
            if node_id is None and not shared and client.user_id != actor_id:
                continue
            if len(client.pending) >= MAX_PENDING:
                client.overflow = True
            else:
                client.pending.add((signal, node_id))
            client.wake.set()

    async def person(self, app: Any, user_id: str) -> Principal:
        """Return the person's current grants (read off the event loop, cached for a second)."""
        now = time.monotonic()
        cached = self.grants_cache.get(user_id)
        if cached and now - cached[0] < GRANTS_CACHE_SECONDS:
            return cached[1]

        def load() -> Principal:
            conn = app.state.settings.open_db()
            try:
                return app.state.registry.principal_for(conn, user_id)
            finally:
                conn.close()

        person = await asyncio.to_thread(load)
        self.grants_cache[user_id] = (now, person)
        if len(self.grants_cache) > MAX_CLIENTS:
            self.grants_cache = {u: v for u, v in self.grants_cache.items() if now - v[0] < 5}
        return person


def may_hear(person: Principal, definition: Signal, node_id: str | None, catalogue: dict) -> bool:
    """Whether this person may be told: the signal's right on the machine, or anywhere."""
    if node_id is not None:
        return person.has(definition.right, catalogue, f"node:{node_id}")
    return any(person.has(definition.right, catalogue, g.scope) for g in person.grants)


async def deliver(app: Any, hub: RealtimeHub, client: Client) -> None:
    """Send this client its queued signals until the connection ends."""
    catalogue = app.state.registry.rights()
    definitions = app.state.registry.signals()
    while True:
        await client.wake.wait()
        await asyncio.sleep(BATCH_SECONDS)  # let a burst finish, then send it as one batch
        client.wake.clear()
        if client.overflow:
            # Too far behind to be told one by one: "everything may have changed".
            client.overflow = False
            client.pending.clear()
            await _send(client, {"signal": "*", "node": None})
            continue
        batch, client.pending = client.pending, set()
        if not batch:
            continue
        person = await hub.person(app, client.user_id)
        for signal, node_id in sorted(batch, key=lambda s: (s[0], s[1] or "")):
            definition = definitions.get(signal)
            if definition is not None and may_hear(person, definition, node_id, catalogue):
                await _send(client, {"signal": signal, "node": node_id})


async def _send(client: Client, message: dict[str, Any]) -> None:
    """Send one message; a client that does not read in time is disconnected."""
    try:
        await asyncio.wait_for(client.ws.send_json(message), SEND_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001  (any failure means this connection is done)
        with contextlib.suppress(Exception):
            await client.ws.close(code=1011)
        raise
