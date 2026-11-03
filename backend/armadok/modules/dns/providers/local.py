"""Local DNS: no account, nothing is published.

The domain only gives names to sites and to rights (a Pi-hole, an AdGuard or a router that
serves these names, say). Armadok cannot read or change what that DNS answers.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from armadok.modules.dns.base import ProviderError, Record


class LocalCredentials(BaseModel):
    """A local DNS has no credentials."""


class LocalProvider:
    """Provider with no capabilities: every operation that would publish is refused."""

    capabilities: ClassVar[frozenset[str]] = frozenset()
    credentials: ClassVar[type[BaseModel]] = LocalCredentials

    def __init__(self, credentials: BaseModel) -> None:
        """Nothing to keep."""

    async def verify(self) -> None:
        """Accept: there is nothing to check."""

    async def list_zones(self) -> list[str]:
        """Return nothing: a local DNS knows no zones."""
        return []

    async def list_records(self, zone: str) -> list[Record]:
        """Return nothing: records cannot be read."""
        return []

    async def get_record(self, zone: str, record_id: str) -> Record | None:
        """Return nothing: records cannot be read."""
        return None

    async def create_record(self, zone: str, record: Record) -> Record:
        """Refuse: nothing is published by a local DNS."""
        raise ProviderError("dns.not_supported")

    async def update_record(self, zone: str, record_id: str, record: Record) -> Record:
        """Refuse: nothing is published by a local DNS."""
        raise ProviderError("dns.not_supported")

    async def delete_record(self, zone: str, record_id: str) -> None:
        """Refuse: nothing is published by a local DNS."""
        raise ProviderError("dns.not_supported")
