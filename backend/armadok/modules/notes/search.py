"""Search provider of the notes module: the caller's own notes, and nobody else's."""

from __future__ import annotations

import collections
import threading
import time

from armadok.core.module import SearchContext, SearchHit
from armadok.modules.notes import repository

MIN_LENGTH = 3  # shorter texts match nearly everything and are not worth decrypting a mailbox for
CALLS_PER_MINUTE = 10  # decrypting every note is the costliest search there is
_budget: dict[str, collections.deque] = {}
_lock = threading.Lock()
_busy = threading.BoundedSemaphore(4)  # at most this many scans at once on the whole hub


def _allowed(who_id: str) -> bool:
    now = time.monotonic()
    with _lock:
        if len(_budget) > 5000:
            for key in [k for k, d in _budget.items() if not d or now - d[-1] > 60]:
                del _budget[key]
        recent = _budget.setdefault(who_id, collections.deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= CALLS_PER_MINUTE:
            return False
        recent.append(now)
        return True


def run(ctx: SearchContext, text: str, limit: int) -> list[SearchHit]:
    """Return the caller's notes whose text contains `text` (notes are entirely private).

    Only for a person's own session: a key never reads notes through the search. A note that
    cannot be read is skipped, not fatal.
    """
    if ctx.who.kind != "session" or len(text) < MIN_LENGTH or not _allowed(ctx.who.id):
        return []
    if not _busy.acquire(blocking=False):
        return []
    try:
        rows = ctx.conn.execute(
            "SELECT * FROM notes WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ?",
            (ctx.who.id, repository.MAX_PER_OWNER),
        ).fetchall()
        hits = []
        for row in rows:
            try:
                note = repository._public(ctx.settings, row)  # noqa: SLF001
            except Exception:  # noqa: BLE001  (one damaged note must not hide the others)
                continue
            if text in note["body"].lower():
                first_line = note["body"].strip().splitlines()[0] if note["body"].strip() else ""
                hits.append(SearchHit("note", first_line[:80], "/notes", note["object_type"]))
                if len(hits) >= limit:
                    break
        return hits
    finally:
        _busy.release()
