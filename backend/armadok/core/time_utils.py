"""Small time helpers shared by modules."""

from __future__ import annotations

from datetime import UTC, datetime


def still_valid(expires_at: datetime | None) -> bool:
    """Return True for a right that never expires or whose expiry is still in the future."""
    return expires_at is None or expires_at > datetime.now(UTC)
