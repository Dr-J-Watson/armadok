"""Rules shared by every way of creating a key (a person's own, a service account's)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from armadok.core.errors import ApiError
from armadok.core.module import Right


def held_rights(grants: tuple[Any, ...]) -> set[str]:
    """Return the names of the rights in `grants` that have not expired."""
    now = datetime.now(UTC)
    return {g.right for g in grants if g.expires_at is None or g.expires_at > now}


def check_rights(held: set[str], catalogue: dict[str, Right], requested: list[str]) -> list[str]:
    """Return the sorted rights for a new key, or raise.

    Each must exist, not be sensitive, be held now by whoever the key will act for, and come
    with its dependencies (a right without them would silently not work).
    """
    wanted = set(requested)
    for right in wanted:
        if right not in catalogue or catalogue[right].sensitive or right not in held:
            raise ApiError("apikeys.invalid_right", 422)
        if not set(catalogue[right].depends_on) <= wanted:
            raise ApiError("apikeys.dependency_missing", 422)
    return sorted(wanted)


def check_expiry(expires_at: datetime | None) -> None:
    """Refuse an end date that is already past."""
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ApiError("apikeys.invalid_expiry", 422)
