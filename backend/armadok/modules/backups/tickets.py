"""One-time download tickets for backup archives.

An archive is downloaded by a plain browser request, which cannot carry the access token, so
the person first asks for a ticket (proving who they are, with a fresh second-factor code) and
then opens the download address with it. A ticket works once, for one archive on one machine,
for a short time; only its hash is kept, in memory. Nothing of the archive is stored on the hub:
it is read piece by piece from the machine and passed on.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

LIFETIME_SECONDS = 60
MAX_PENDING = 200


@dataclass(frozen=True)
class Ticket:
    """Whom a ticket was issued to and for what."""

    user_id: str
    node_id: str
    archive: str
    expires: float


def _store(app: Any) -> dict[str, Ticket]:
    if not hasattr(app.state, "download_tickets"):
        app.state.download_tickets = {}
    return app.state.download_tickets


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(app: Any, user_id: str, node_id: str, archive: str) -> str | None:
    """Create a ticket and return its secret, or None when too many are waiting."""
    store, now = _store(app), time.monotonic()
    for key in [k for k, t in store.items() if t.expires <= now]:
        del store[key]
    if len(store) >= MAX_PENDING:
        return None
    token = secrets.token_urlsafe(32)
    store[_key(token)] = Ticket(user_id, node_id, archive, now + LIFETIME_SECONDS)
    return token


def redeem(app: Any, token: str, node_id: str, archive: str) -> Ticket | None:
    """Use a ticket: it must exist, be unexpired and be for exactly this archive. Single use."""
    store, key = _store(app), _key(token)
    ticket = store.get(key)
    if ticket is None or (ticket.node_id, ticket.archive) != (node_id, archive):
        return None  # a ticket presented for the wrong archive is not used up by the attempt
    del store[key]
    return None if ticket.expires <= time.monotonic() else ticket
