"""DuckDNS: one address (IPv4 and/or IPv6) and one TXT value per `name.duckdns.org`.

DuckDNS has no way to read what is set, so records are shown from what the name resolves to
(A and AAAA); the TXT value cannot be read. Writing goes through its update URL. Sub-domains of
a DuckDNS name follow the same address by themselves.
"""

from __future__ import annotations

import asyncio
import socket
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from armadok.modules.dns.base import ProviderError, Record

BASE_URL = "https://www.duckdns.org"
SUFFIX = ".duckdns.org"


class DuckDnsCredentials(BaseModel):
    """The account token (a UUID shown on the DuckDNS home page)."""

    token: str = Field(pattern=r"^[0-9a-fA-F-]{20,64}$", repr=False)


class DuckDnsProvider:
    """DuckDNS provider: set the address and the TXT value of a name."""

    capabilities: ClassVar[frozenset[str]] = frozenset({"records", "publish_ip"})
    credentials: ClassVar[type[BaseModel]] = DuckDnsCredentials

    def __init__(self, credentials: BaseModel) -> None:
        """Keep the token."""
        self._token = credentials.token  # type: ignore[attr-defined]

    @staticmethod
    def _name(zone: str) -> str:
        if not zone.endswith(SUFFIX) or zone.count(".") != 2:
            raise ProviderError("dns.invalid_zone")
        return zone[: -len(SUFFIX)]

    async def _update(self, zone: str, **params: str) -> None:
        query = {"domains": self._name(zone), "token": self._token, "verbose": "false", **params}
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                answer = await client.get(f"{BASE_URL}/update", params=query)
        except httpx.HTTPError:
            raise ProviderError("dns.unreachable") from None
        if answer.text.strip() != "OK":
            raise ProviderError("dns.rejected")

    async def verify(self) -> None:
        """DuckDNS has no login check: the token is only tried when something is set."""

    async def list_zones(self) -> list[str]:
        """DuckDNS cannot list an account's names."""
        return []

    async def list_records(self, zone: str) -> list[Record]:
        """Show the addresses the name resolves to (the TXT value cannot be read)."""
        self._name(zone)
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(zone, None, type=socket.SOCK_STREAM), 3
            )
        except (OSError, TimeoutError):
            return []
        found: dict[tuple[str, str], Record] = {}
        for family, _, _, _, sockaddr in infos:
            kind = "AAAA" if family == socket.AF_INET6 else "A"
            found[(kind, sockaddr[0])] = Record(f"{kind}:{sockaddr[0]}", kind, "", sockaddr[0], 60)
        return list(found.values())

    async def get_record(self, zone: str, record_id: str) -> Record | None:
        """Return the record with this id (`TYPE:value`) if the name currently has it."""
        for record in await self.list_records(zone):
            if record.id == record_id:
                return record
        return None

    async def create_record(self, zone: str, record: Record) -> Record:
        """Set the address (A or AAAA) or the TXT value of the name."""
        if record.name != "":
            raise ProviderError("dns.not_supported")  # sub-domains follow the same address
        params = {
            "A": {"ip": record.target},
            "AAAA": {"ipv6": record.target},
            "TXT": {"txt": record.target},
        }
        if record.type not in params:
            raise ProviderError("dns.not_supported")
        await self._update(zone, **params[record.type])
        return Record(f"{record.type}:{record.target}", record.type, "", record.target, record.ttl)

    async def update_record(self, zone: str, record_id: str, record: Record) -> Record:
        """Set the value: DuckDNS only has "set", so this is the same as creating."""
        return await self.create_record(zone, record)

    async def delete_record(self, zone: str, record_id: str) -> None:
        """Clear the TXT value (the address cannot be removed, only changed)."""
        if not record_id.startswith("TXT:"):
            raise ProviderError("dns.not_supported")
        await self._update(zone, txt="", clear="true")
