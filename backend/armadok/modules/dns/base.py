"""What a DNS provider is: the interface every provider implements, and the shared types.

A provider declares its `capabilities`; the rest of the application checks capabilities and
never a provider's name. Adding a provider = one new file under `providers/` and one line in
`module.py`; nothing else changes.

Capabilities in use:
- `records`  the provider can list the records of a zone;
- `write`    it can create, change and delete records;
- `zones`    it can list the zones its account owns (so they can be imported);
- `publish_ip`  it can only set the address of a name (DuckDNS style), not edit records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Protocol

from pydantic import BaseModel

RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA")
DOMAIN_PATTERN = r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
LABEL = r"[a-z0-9_*](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
# "" is the zone's apex; otherwise one or more labels, `*` allowed for a wildcard.
SUBDOMAIN_PATTERN = rf"^(?:{LABEL}(?:\.{LABEL})*)?$"
_NAME = re.compile(DOMAIN_PATTERN)


def is_domain(name: str) -> bool:
    """Return True if `name` looks like a registrable domain name (lower case)."""
    return len(name) <= 253 and bool(_NAME.fullmatch(name))


class ProviderError(Exception):
    """A provider refused or failed; `code` is a stable, translatable error code."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Record:
    """One DNS record. `name` is relative to the zone ("" for the apex)."""

    id: str
    type: str
    name: str
    target: str
    ttl: int


class DnsProvider(Protocol):
    """The interface of a DNS provider (see the module docstring for capabilities)."""

    capabilities: ClassVar[frozenset[str]]
    credentials: ClassVar[type[BaseModel]]

    def __init__(self, credentials: BaseModel) -> None:
        """Build the provider from validated credentials (never logged, never returned)."""

    async def verify(self) -> None:
        """Check the credentials work; raise `ProviderError` if not."""

    async def list_zones(self) -> list[str]:
        """Return the zones of the account (empty if the provider cannot say)."""

    async def list_records(self, zone: str) -> list[Record]:
        """Return the records of a zone."""

    async def get_record(self, zone: str, record_id: str) -> Record | None:
        """Return one record by id, or None if it does not exist."""

    async def create_record(self, zone: str, record: Record) -> Record:
        """Create a record (its `id` is ignored) and return it as stored."""

    async def update_record(self, zone: str, record_id: str, record: Record) -> Record:
        """Replace a record and return it as stored."""

    async def delete_record(self, zone: str, record_id: str) -> None:
        """Delete a record."""


def valid_target(record_type: str, target: str) -> bool:
    """Check a record value is plausible for its type (so errors are clear, not the provider's)."""
    import ipaddress

    try:
        if record_type == "A":
            return isinstance(ipaddress.ip_address(target), ipaddress.IPv4Address)
        if record_type == "AAAA":
            return isinstance(ipaddress.ip_address(target), ipaddress.IPv6Address)
    except ValueError:
        return False
    if record_type in ("CNAME", "NS"):
        return (
            bool(
                re.fullmatch(
                    r"(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*[A-Za-z0-9_-]{1,63}\.?",
                    target,
                )
            )
            and len(target) <= 253
        )
    return True
