"""Judging certificates: which name a certificate covers, and how long it has left."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from armadok.modules.certs.schemas import Certificate

WARN_DAYS = 21
CRITICAL_DAYS = 7


def covers(name: str, host: str) -> bool:
    """Return True if a certificate name covers `host`.

    A wildcard `*.example.com` covers exactly one label below `example.com`, as in TLS.
    """
    name, host = name.lower(), host.lower()
    if name == host:
        return True
    if name.startswith("*."):
        head, _, tail = host.partition(".")
        return bool(head) and tail == name[2:]
    return False


def days_left(cert: Certificate, now: datetime | None = None) -> int:
    """Return whole days until expiry (negative once expired)."""
    delta = cert.not_after - (now or datetime.now(UTC))
    return delta.days if delta.total_seconds() >= 0 else -((-delta).days + 1)


def state(days: int) -> str:
    """Return "ok", "expiring" or "expired"."""
    if days < 0:
        return "expired"
    return "expiring" if days < WARN_DAYS else "ok"


def best_cover(host: str, certs: list[Certificate]) -> Certificate | None:
    """Return the valid certificate that covers `host` and lasts longest (else any that covers)."""
    matching = [c for c in certs if any(covers(n, host) for n in c.names)]
    return max(matching, key=lambda c: c.not_after, default=None)


def describe(
    cert: Certificate, now: datetime | None = None, names: list[str] | None = None
) -> dict[str, Any]:
    """Return a certificate as the API shows it, with its remaining time.

    `names` limits which of the certificate's names are shown (a multi-domain certificate must
    not reveal names the viewer has no right to see); `serial` is dropped in that case too.
    """
    left = days_left(cert, now)
    return {
        "resolver": cert.resolver,
        "names": cert.names if names is None else names,
        "issuer": cert.issuer,
        "not_before": cert.not_before.isoformat(),
        "not_after": cert.not_after.isoformat(),
        "serial": cert.serial if names is None else None,
        "days_left": left,
        "state": state(left),
    }
