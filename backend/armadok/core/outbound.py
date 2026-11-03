"""Outgoing HTTP requests to addresses chosen by users (webhooks and the like).

A user who can add a notification channel chooses a URL the hub will call. Left unchecked that
is a way to make the hub reach services only it can see (cloud metadata, internal admin pages).
So: only http(s), no credentials in the URL, the name is resolved here and every address must
be public (unless the operator allowed private ones), the request goes to the address we
checked (not to a second lookup), redirects are not followed, the whole exchange has a
deadline and the answer is never read.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

TIMEOUT_SECONDS = 10  # per phase (connect, write, read)
DEADLINE_SECONDS = 15  # for the whole request: a server dripping bytes is cut off
MAX_URL_LENGTH = 2000


class OutboundError(Exception):
    """A request was refused or failed; `code` is a stable, translatable error code."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        super().__init__(code)
        self.code = code


# Ranges `is_global` still lets through but that reach internal hosts through a gateway.
_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "192.0.0.0/24")
)


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return False  # a mapped address is how internal hosts hide in an IPv6 literal
    if any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version):
        return False
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int, allow_private: bool) -> str:
    """Return one address for `host`, refusing the name if ANY of its addresses is private."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise OutboundError("outbound.unresolvable") from None
    addresses = [info[4][0] for info in infos]
    if not addresses or (not allow_private and not all(_public(a) for a in addresses)):
        raise OutboundError("outbound.address_not_allowed")
    return addresses[0]


def check_url(url: str) -> tuple[str, str, int, str]:
    """Validate a URL's shape; return `(scheme, host, port, path_and_query)`."""
    if len(url) > MAX_URL_LENGTH:
        raise OutboundError("outbound.invalid_url")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise OutboundError("outbound.invalid_url") from None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise OutboundError("outbound.invalid_url")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80), path


async def post(
    url: str,
    content: bytes,
    headers: dict[str, str],
    allow_private: bool = False,
) -> int:
    """POST `content` to `url` and return the HTTP status. Raises `OutboundError`."""
    scheme, host, port, path = check_url(url)
    address = await _resolve(host, port, allow_private)
    target = (
        f"{scheme}://[{address}]:{port}{path}"
        if ":" in address
        else f"{scheme}://{address}:{port}{path}"
    )
    default_port = 443 if scheme == "https" else 80
    host_header = host if port == default_port else f"{host}:{port}"
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            # trust_env=False: proxy variables in the environment must not reroute a request
            # whose destination we just checked.
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                # Streamed and never read: only the status matters, and a huge answer must not
                # be pulled into memory.
                async with client.stream(
                    "POST",
                    target,
                    content=content,
                    headers={**headers, "Host": host_header},
                    # TLS: send and verify the certificate for the real name, not the address.
                    extensions={"sni_hostname": host},
                ) as response:
                    return response.status_code
    except (httpx.HTTPError, TimeoutError):
        raise OutboundError("outbound.failed") from None
