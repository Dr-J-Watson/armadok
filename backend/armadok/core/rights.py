"""Rights evaluation: grants, scopes, dependencies and expiry.

Pure logic with no database access, so it can be tested alone. A `Principal` is
whoever acts (a person, an API key, a service account) with the grants it holds.
Nothing here gives an administrator implicit access: only explicit grants count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from armadok.core.errors import MissingRight
from armadok.core.module import Right

# Scope meaning "everywhere"; any other scope is a "<kind>:<id>" string, e.g. "node:abc".
ALL_SCOPES = "*"


@dataclass(frozen=True)
class Grant:
    """One right given to a principal, for one scope, optionally until `expires_at`."""

    right: str
    scope: str = ALL_SCOPES
    expires_at: datetime | None = None

    def covers(self, scope: str, now: datetime) -> bool:
        """Return True if this grant is still valid and applies to `scope`."""
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return self.scope == ALL_SCOPES or self.scope == scope


@dataclass(frozen=True)
class Principal:
    """The identity performing an action, with the grants it currently holds."""

    id: str
    grants: tuple[Grant, ...] = ()
    # "session" for a logged-in person, "api_key" for a key acting on behalf of `id`.
    kind: str = "session"
    key_id: str | None = None
    # A read-only key may only use safe HTTP methods (enforced in `get_principal`).
    read_only: bool = False

    @property
    def actor(self) -> str:
        """Identity written to the audit log: the account, plus the key if one was used."""
        return f"{self.id} via key:{self.key_id}" if self.key_id else self.id

    def has(
        self,
        right: str,
        catalogue: dict[str, Right],
        scope: str = ALL_SCOPES,
        now: datetime | None = None,
    ) -> bool:
        """Return True if `right` is held for `scope`, with all its dependencies.

        A right whose dependencies are missing is unusable even if granted,
        e.g. "write" without "read".
        """
        now = now or datetime.now(UTC)
        if right not in catalogue:
            return False
        needed = (right, *catalogue[right].depends_on)
        return all(self._holds(name, scope, now) for name in needed)

    def _holds(self, right: str, scope: str, now: datetime) -> bool:
        return any(g.right == right and g.covers(scope, now) for g in self.grants)

    def require(
        self,
        right: str,
        catalogue: dict[str, Right],
        scope: str = ALL_SCOPES,
        now: datetime | None = None,
    ) -> None:
        """Raise `MissingRight` unless `has(...)` is true."""
        if not self.has(right, catalogue, scope, now):
            raise MissingRight()


def can_grant(
    granter: Principal,
    grant: Grant,
    catalogue: dict[str, Right],
    now: datetime | None = None,
) -> bool:
    """Return True if `granter` may give `grant` to someone else.

    Nobody can grant more than they hold: same right, a scope they cover, and
    an expiry no later than their own. A grant that never expires needs a
    holder grant that never expires.
    """
    for held in granter.grants:
        if held.right != grant.right:
            continue
        if held.scope != ALL_SCOPES and held.scope != grant.scope:
            continue
        if held.expires_at is not None and (
            grant.expires_at is None or grant.expires_at > held.expires_at
        ):
            continue
        return granter.has(grant.right, catalogue, grant.scope, now)
    return False
