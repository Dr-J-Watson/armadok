"""The global search endpoint.

It asks each module's provider for objects matching a text. Every provider reads only what the
caller may see, none of them talks to a worker, and a provider that fails is skipped: one
broken module never empties the search. Calls are rate limited per account.
"""

from __future__ import annotations

import collections
import logging
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.module import SearchContext
from armadok.core.registry import Registry
from armadok.core.rights import Principal

router = APIRouter(tags=["search"])
log = logging.getLogger("armadok.search")

MIN_LENGTH = 2
PER_PROVIDER = 8
CALLS_PER_MINUTE = 60
MAX_TRACKED = 5000
_calls: dict[str, collections.deque] = {}
_lock = threading.Lock()


def _within_rate(who_id: str) -> bool:
    """Count a call against the account's budget; False when it is spent."""
    now = time.monotonic()
    with _lock:
        if len(_calls) > MAX_TRACKED:
            # Forget only the accounts that have been quiet for a minute, never everyone's count.
            for key in [k for k, d in _calls.items() if not d or now - d[-1] > 60]:
                del _calls[key]
            if len(_calls) > MAX_TRACKED and who_id not in _calls:
                return False
        recent = _calls.setdefault(who_id, collections.deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= CALLS_PER_MINUTE:
            return False
        recent.append(now)
        return True


class SearchIn(BaseModel):
    """The text to look for (in the body, not the address: it may be a piece of a private note)."""

    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=MIN_LENGTH, max_length=64)


@router.post("/search", summary="Search objects by name")
def search(
    body: SearchIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the objects whose name contains the text, among those you may see.

    It is a POST only to keep the text out of URLs and access logs; nothing is changed.
    """
    text = body.q.strip().lower()
    if len(text) < MIN_LENGTH:
        raise ApiError("search.too_short", 422)
    if not _within_rate(who.id):
        raise ApiError("search.too_fast", 429)
    ctx = SearchContext(conn, registry, who, request.app.state.settings)
    results = []
    for provider in registry.search_providers():
        try:
            hits = provider.run(ctx, text, PER_PROVIDER)
        except Exception:  # noqa: BLE001  (a broken provider must not break the search)
            log.exception("search provider %s failed", provider.name)
            continue
        results.extend(
            {"type": h.type, "label": h.label[:120], "detail": h.detail[:160], "to": h.to}
            for h in hits[:PER_PROVIDER]
        )
    return {"results": results}
