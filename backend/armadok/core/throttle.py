"""Slowing down wrong guesses, kept in memory.

Used wherever an unauthenticated caller presents a secret (a hook token, an OAuth2 client
secret). Failures are counted per key (an address, a hook, a client) over the last minute; once
a key has too many, further *wrong* guesses are refused with a "too many" answer. The right
secret is never blocked by someone else's guessing: callers check the secret first and only
consult the limiter when it was wrong.
"""

from __future__ import annotations

import time
from collections import deque


class FailureLimiter:
    """Counts recent failures per key, with bounded memory.

    Each kind of key (`ip`, `hook`, `client`...) has its own table, so flooding one kind with
    made-up values (thousands of client ids) cannot push out the counters of another (addresses).
    """

    def __init__(self, limits: dict[str, int], max_tracked: int = 5000) -> None:
        """`limits` maps a kind to the failures per minute one key of that kind may have."""
        self.limits = limits
        self.max_tracked = max_tracked
        self._seen: dict[str, dict[str, deque[float]]] = {kind: {} for kind in limits}

    def _window(self, kind: str, value: str) -> deque[float]:
        now = time.monotonic()
        table = self._seen[kind]
        window = table.setdefault(value, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(table) > self.max_tracked:
            # Forget whoever has been quiet for a minute; if that is not enough, the oldest half.
            for stale in [k for k, v in table.items() if not v or now - v[-1] > 60]:
                del table[stale]
            if len(table) > self.max_tracked:
                for old in list(table)[: self.max_tracked // 2]:
                    del table[old]
            window = table.setdefault(value, window)
        return window

    def blocked(self, **keys: str) -> bool:
        """Whether any of the keys (`ip="..."`, `hook="..."`) is over its limit."""
        return any(len(self._window(k, v)) >= self.limits[k] for k, v in keys.items())

    def remember(self, **keys: str) -> None:
        """Count one failure against each key."""
        now = time.monotonic()
        for kind, value in keys.items():
            self._window(kind, value).append(now)

    def clear(self) -> None:
        """Forget everything (tests)."""
        for table in self._seen.values():
            table.clear()

    def size(self) -> int:
        """Return the number of keys currently tracked."""
        return sum(len(table) for table in self._seen.values())
