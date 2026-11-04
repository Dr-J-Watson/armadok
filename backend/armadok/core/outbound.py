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
import smtplib
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
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


async def _pinned(url: str, allow_private: bool) -> tuple[str, str, str]:
    """Check a URL and return `(address URL to connect to, Host header, name for TLS)`."""
    scheme, host, port, path = check_url(url)
    address = await _resolve(host, port, allow_private)
    target = (
        f"{scheme}://[{address}]:{port}{path}"
        if ":" in address
        else f"{scheme}://{address}:{port}{path}"
    )
    default_port = 443 if scheme == "https" else 80
    return target, (host if port == default_port else f"{host}:{port}"), host


async def post(
    url: str,
    content: bytes,
    headers: dict[str, str],
    allow_private: bool = False,
) -> int:
    """POST `content` to `url` and return the HTTP status. Raises `OutboundError`."""
    target, host_header, host = await _pinned(url, allow_private)
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


async def fetch(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    allow_private: bool = False,
    max_bytes: int = 64 * 1024,
) -> tuple[int, dict[str, str], bytes]:
    """Send a GET or HEAD and return `(status, lower-cased headers, body)`.

    Same guards as `post` (public addresses only unless allowed, the address that was checked,
    no redirect, one deadline). The body is read only up to `max_bytes`: a longer one is an
    error, never silently cut. Raises `OutboundError`.
    """
    if method not in ("GET", "HEAD"):
        raise OutboundError("outbound.invalid_url")
    target, host_header, host = await _pinned(url, allow_private)
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    method,
                    target,
                    headers={**headers, "Host": host_header},
                    extensions={"sni_hostname": host},
                ) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise OutboundError("outbound.too_large")
                        chunks.append(chunk)
                    return (
                        response.status_code,
                        {k.lower(): v for k, v in response.headers.items()},
                        b"".join(chunks),
                    )
    except (httpx.HTTPError, TimeoutError):
        raise OutboundError("outbound.failed") from None


# --- mail ---------------------------------------------------------------------------------

MAIL_DEADLINE_SECONDS = 20
MAX_REPLY_BYTES = 256 * 1024  # all the server may say in one session
# Outside a private network the hub only speaks to the ports mail servers use for submission.
MAIL_PORTS = frozenset({25, 465, 587, 2525})
_MAIL_THREADS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="armadok-mail")


def check_mail_port(port: int, allow_private: bool) -> None:
    """Refuse a port that is not a mail port, unless private addresses are allowed."""
    if not allow_private and port not in MAIL_PORTS:
        raise OutboundError("outbound.port_not_allowed")


class _CappedFile:
    """The reading side of an SMTP connection, with a cap on everything the server may send."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self._total = 0

    def readline(self, limit: int = -1) -> bytes:
        line = self._inner.readline(limit)
        self._total += len(line)
        if self._total > MAX_REPLY_BYTES:
            raise OSError("the server said too much")
        return line

    def close(self) -> None:
        self._inner.close()


def _smtp_classes(address: str, opened: list):
    """Return SMTP classes that dial `address` (already checked) yet verify TLS for the name.

    The name given to the class stays the one the certificate is checked against; only the
    place the socket connects to is pinned, so a second DNS lookup cannot send us elsewhere.
    Every socket opened is noted in `opened`, so a watchdog can cut a conversation short, and
    what the server sends is capped.
    """

    class Pinned(smtplib.SMTP):
        def _get_socket(self, host, port, timeout):  # noqa: ANN001
            raw = socket.create_connection((address, port), timeout)
            opened.append(raw)
            return raw

        def getreply(self):
            if self.file is None:
                self.file = _CappedFile(self.sock.makefile("rb"))
            return super().getreply()

    class PinnedSSL(smtplib.SMTP_SSL):
        def _get_socket(self, host, port, timeout):  # noqa: ANN001
            raw = socket.create_connection((address, port), timeout)
            opened.append(raw)
            return self.context.wrap_socket(raw, server_hostname=self._host)

        def getreply(self):
            if self.file is None:
                self.file = _CappedFile(self.sock.makefile("rb"))
            return super().getreply()

    return Pinned, PinnedSSL


def _cut(opened: list) -> None:
    """Close every socket of a conversation; a thread blocked reading from one wakes up."""
    for sock in opened:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def _deliver_mail(
    address: str,
    *,
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    message: EmailMessage,
) -> None:
    opened: list = []
    plain, secure = _smtp_classes(address, opened)
    context = ssl.create_default_context()
    # smtplib's timeout is per read: a server that answers slowly, or endlessly, would hold this
    # thread for ever. After the deadline the sockets are closed and the thread ends.
    watchdog = threading.Timer(MAIL_DEADLINE_SECONDS, _cut, args=(opened,))
    watchdog.daemon = True
    watchdog.start()
    try:
        if security == "ssl":
            client = secure(
                host, port, local_hostname="localhost", timeout=TIMEOUT_SECONDS, context=context
            )
        else:
            client = plain(host, port, local_hostname="localhost", timeout=TIMEOUT_SECONDS)
        with client:
            if security == "starttls":
                client.starttls(context=context)
            if username:
                client.login(username, password)
            client.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise OutboundError("outbound.smtp_auth") from None
    except (smtplib.SMTPException, OSError, ssl.SSLError):
        raise OutboundError("outbound.failed") from None
    finally:
        watchdog.cancel()
        _cut(opened)


async def send_mail(
    *,
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    allow_private: bool = False,
) -> None:
    """Send one plain-text message through an SMTP server chosen by a user.

    The server's name is resolved here and must be public (unless private addresses are
    allowed); the connection goes to that very address; TLS is verified for the real name;
    a password is never sent without TLS; the whole exchange has a deadline; nothing of the
    server's answer is kept. Raises `OutboundError`.
    """
    if security == "none" and (username or password):
        raise OutboundError("outbound.invalid_mail")
    check_mail_port(port, allow_private)
    address = await _resolve(host, port, allow_private)
    message = EmailMessage()
    try:
        # EmailMessage refuses a line break in a header: nothing can add a header of its own.
        message["From"], message["To"], message["Subject"] = sender, recipient, subject
    except ValueError:
        raise OutboundError("outbound.invalid_mail") from None
    message.set_content(body)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _MAIL_THREADS,
                lambda: _deliver_mail(
                    address,
                    host=host,
                    port=port,
                    security=security,
                    username=username,
                    password=password,
                    message=message,
                ),
            ),
            MAIL_DEADLINE_SECONDS + 3,  # backstop: the watchdog above ends the thread itself
        )
    except TimeoutError:
        raise OutboundError("outbound.failed") from None
