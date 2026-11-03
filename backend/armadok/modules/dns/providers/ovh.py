"""OVH: the whole zone, through the OVH API (create, change and delete any record).

Requests are signed as OVH asks (application key and secret, consumer key, timestamp). Changes
are published with a zone refresh right after each write. One HTTP client is shared by a whole
operation, records are fetched a few at a time, and the whole operation has a deadline, because
listing a zone costs one request per record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from armadok.modules.dns.base import ProviderError, Record

# Fixed list: the account holder cannot make the hub call an address of their choosing.
ENDPOINTS = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
}
DEFAULT_MAX_RECORDS = 500
HARD_MAX_RECORDS = 5000  # listing costs one request per record: this is the ceiling whatever is set


def max_records() -> int:
    """Return how many records of one zone we are willing to read.

    `ARMADOK_DNS_MAX_RECORDS` raises or lowers it (1 to 5000, default 500). Reading a zone takes
    one OVH request per record, so a large zone is slow and uses the account's API quota.
    """
    try:
        value = int(os.environ.get("ARMADOK_DNS_MAX_RECORDS", DEFAULT_MAX_RECORDS))
    except ValueError:
        value = DEFAULT_MAX_RECORDS
    return max(1, min(value, HARD_MAX_RECORDS))


CONCURRENCY = 8
DEADLINE_SECONDS = 60
MAX_ANSWER_BYTES = 2 * 1024 * 1024
_RECORD_ID = re.compile(r"[0-9]{1,20}")


class OvhCredentials(BaseModel):
    """The three keys of an OVH API application, and the region."""

    endpoint: Literal["ovh-eu", "ovh-ca", "ovh-us"] = "ovh-eu"
    application_key: str = Field(pattern=r"^[A-Za-z0-9]{8,64}$")
    application_secret: str = Field(pattern=r"^[A-Za-z0-9]{16,64}$", repr=False)
    consumer_key: str = Field(pattern=r"^[A-Za-z0-9]{16,64}$", repr=False)


def _check_id(record_id: str) -> str:
    """Accept only a numeric OVH record id: it is put into a URL that is signed."""
    if not _RECORD_ID.fullmatch(record_id):
        raise ProviderError("dns.invalid_record")
    return record_id


class OvhProvider:
    """OVH provider with the full record API."""

    capabilities: ClassVar[frozenset[str]] = frozenset({"records", "write", "zones"})
    credentials: ClassVar[type[BaseModel]] = OvhCredentials

    def __init__(self, credentials: BaseModel) -> None:
        """Keep the keys and compute the endpoint."""
        self._c = credentials
        self._base = ENDPOINTS[credentials.endpoint]  # type: ignore[attr-defined]
        self._delta: int | None = None
        self._client: httpx.AsyncClient | None = None

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = self._base + path
        payload = "" if body is None else json.dumps(body, separators=(",", ":"))
        c = self._c
        client = self._client or httpx.AsyncClient(timeout=15, trust_env=False)
        try:
            if self._delta is None:  # OVH rejects a clock that is off: align with theirs
                server = await client.get(f"{self._base}/auth/time")
                self._delta = int(server.text) - int(time.time())
            stamp = str(int(time.time()) + self._delta)
            signature = (
                "$1$"
                + hashlib.sha1(  # noqa: S324  (the scheme OVH requires)
                    "+".join(
                        [c.application_secret, c.consumer_key, method, url, payload, stamp]  # type: ignore[attr-defined]
                    ).encode()
                ).hexdigest()
            )
            answer = await client.request(
                method,
                url,
                content=payload or None,
                headers={
                    "X-Ovh-Application": c.application_key,  # type: ignore[attr-defined]
                    "X-Ovh-Consumer": c.consumer_key,  # type: ignore[attr-defined]
                    "X-Ovh-Timestamp": stamp,
                    "X-Ovh-Signature": signature,
                    "Content-Type": "application/json",
                },
            )
        except (httpx.HTTPError, ValueError):
            raise ProviderError("dns.unreachable") from None
        finally:
            if self._client is None:
                await client.aclose()
        if answer.status_code in (401, 403):
            raise ProviderError("dns.auth_failed")
        if answer.status_code == 404:
            raise ProviderError("dns.not_found")
        if answer.status_code == 429:
            raise ProviderError("dns.rate_limited")
        if answer.status_code >= 400:
            raise ProviderError("dns.rejected")
        if len(answer.content) > MAX_ANSWER_BYTES:
            raise ProviderError("dns.bad_answer")
        try:
            return answer.json() if answer.content else None
        except ValueError:
            raise ProviderError("dns.bad_answer") from None

    @staticmethod
    def _record(raw: Any) -> Record:
        """Read one record from an answer, which is untrusted input."""
        try:
            return Record(
                str(raw["id"]),
                str(raw["fieldType"]),
                str(raw.get("subDomain") or ""),
                str(raw.get("target") or ""),
                int(raw.get("ttl") or 0),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("dns.bad_answer") from None

    async def verify(self) -> None:
        """Try the keys by listing zones."""
        await self._request("GET", "/domain/zone")

    async def list_zones(self) -> list[str]:
        """Return the zones of the account."""
        answer = await self._request("GET", "/domain/zone")
        if not isinstance(answer, list):
            raise ProviderError("dns.bad_answer")
        return [str(z) for z in answer]

    async def list_records(self, zone: str) -> list[Record]:
        """Return all the zone's records.

        One shared client, a few requests at a time, one deadline for everything. A zone with
        more records than we are willing to read is an error, never a silently short list (the
        DNS report would otherwise call existing names "missing").
        """
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            self._client = client
            try:
                async with asyncio.timeout(DEADLINE_SECONDS):
                    ids = await self._request("GET", f"/domain/zone/{zone}/record")
                    if not isinstance(ids, list):
                        raise ProviderError("dns.bad_answer")
                    if len(ids) > max_records():
                        raise ProviderError("dns.too_many_records")
                    slots = asyncio.Semaphore(CONCURRENCY)

                    async def one(record_id: Any) -> Record:
                        async with slots:
                            raw = await self._request(
                                "GET", f"/domain/zone/{zone}/record/{_check_id(str(record_id))}"
                            )
                            return self._record(raw)

                    return list(await asyncio.gather(*(one(i) for i in ids)))
            except TimeoutError:
                raise ProviderError("dns.unreachable") from None
            finally:
                self._client = None

    async def get_record(self, zone: str, record_id: str) -> Record | None:
        """Return one record, or None if it does not exist."""
        try:
            return self._record(
                await self._request("GET", f"/domain/zone/{zone}/record/{_check_id(record_id)}")
            )
        except ProviderError as error:
            if error.code == "dns.not_found":
                return None
            raise

    async def _publish(self, zone: str) -> None:
        await self._request("POST", f"/domain/zone/{zone}/refresh")

    async def create_record(self, zone: str, record: Record) -> Record:
        """Create a record and publish the zone."""
        raw = await self._request(
            "POST",
            f"/domain/zone/{zone}/record",
            {
                "fieldType": record.type,
                "subDomain": record.name,
                "target": record.target,
                "ttl": record.ttl,
            },
        )
        await self._publish(zone)
        return self._record(raw)

    async def update_record(self, zone: str, record_id: str, record: Record) -> Record:
        """Change a record and publish the zone."""
        await self._request(
            "PUT",
            f"/domain/zone/{zone}/record/{_check_id(record_id)}",
            {"subDomain": record.name, "target": record.target, "ttl": record.ttl},
        )
        await self._publish(zone)
        raw = await self._request("GET", f"/domain/zone/{zone}/record/{record_id}")
        return self._record(raw)

    async def delete_record(self, zone: str, record_id: str) -> None:
        """Delete a record and publish the zone."""
        await self._request("DELETE", f"/domain/zone/{zone}/record/{_check_id(record_id)}")
        await self._publish(zone)
