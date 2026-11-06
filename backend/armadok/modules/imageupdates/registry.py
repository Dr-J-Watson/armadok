"""Asking a registry which digest a tag points to today (a `HEAD`, nothing is downloaded).

Registries use a token scheme: an unauthenticated request is answered 401 with a pointer to a
token service; the token is fetched there (with the node's login for that registry when one
is stored and the service may receive it, else anonymously) and the request repeated.
Both hosts go through the outbound guard (public addresses only, unless the operator allowed
private ones), and the digest is checked against a strict pattern before it is trusted.
"""

from __future__ import annotations

import base64
import json
import re
from typing import NamedTuple
from urllib.parse import quote, urlencode, urlsplit

from armadok.core import outbound
from armadok.modules.imageupdates.refs import Ref

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TOKEN = re.compile(r"[\x21-\x7e]{1,8191}")
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


class Login(NamedTuple):
    """A user name and password (or access token) for one registry."""

    username: str
    secret: str


# Registries whose token service lives on another host than the registry itself. A login is
# only ever sent to the registry, to hosts below it, or to one of these known partners: a
# registry that pointed elsewhere (a hostile or hijacked one) cannot collect it.
_TOKEN_PARTNERS = {
    "registry-1.docker.io": {"auth.docker.io"},
    "registry.gitlab.com": {"gitlab.com"},
}


def _may_receive(registry_host: str, realm: str) -> bool:
    """Say whether a login for `registry_host` may be sent to the token service at `realm`."""
    parts = urlsplit(realm)
    host = (parts.hostname or "").lower()
    bare, _, port = registry_host.partition(":")
    if (parts.port or 443) != int(port or 443):
        return False  # another service on the same node must not get the login
    return (
        host == bare or host.endswith("." + bare) or host in _TOKEN_PARTNERS.get(registry_host, ())
    )


def _basic(login: Login) -> str:
    """Return the `Authorization` header value of a login."""
    return "Basic " + base64.b64encode(f"{login.username}:{login.secret}".encode()).decode()


class RegistryError(Exception):
    """The registry could not tell; `code` says why (a stable, translatable code)."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        super().__init__(code)
        self.code = code


async def _head(url: str, headers: dict[str, str], allow_private: bool):
    try:
        return await outbound.fetch("HEAD", url, headers, allow_private=allow_private)
    except outbound.OutboundError as error:
        raise RegistryError(error.code) from None


async def _token(
    challenge: str, repo: str, allow_private: bool, registry_host: str, login: Login | None
) -> str:
    """Fetch a pull token from the service a 401 pointed to (with the login, where it may go)."""
    fields = dict(_CHALLENGE.findall(challenge))
    realm = fields.get("realm", "")
    if not challenge.lower().startswith("bearer") or not realm.startswith("https://"):
        raise RegistryError("images.denied")
    query = {"scope": f"repository:{repo}:pull"}
    if fields.get("service"):
        query["service"] = fields["service"]
    try:
        status, _, body = await outbound.fetch(
            "GET",
            f"{realm}?{urlencode(query)}",
            {"Accept": "application/json"}
            | (
                {"Authorization": _basic(login)}
                if login and _may_receive(registry_host, realm)
                else {}
            ),
            allow_private=allow_private,
        )
    except outbound.OutboundError as error:
        raise RegistryError(error.code) from None
    try:
        document = json.loads(body)
        token = document.get("token") or document.get("access_token")
    except (ValueError, RecursionError, AttributeError, TypeError):
        # A hostile answer (deeply nested, not an object...) is "no token", never a crash.
        raise RegistryError("images.denied") from None
    # Printable ASCII only: the token becomes a header value, so no space, no control
    # character and nothing a header encoder could choke on.
    if status != 200 or not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise RegistryError("images.denied")
    return token


async def current_digest(ref: Ref, allow_private: bool = False, login: Login | None = None) -> str:
    """Return the digest the tag points to now. Raises `RegistryError`.

    Anonymous first. A 401 is answered with a token (fetched with the login when there is one
    and the token service may receive it) or, for a registry that asks for plain credentials,
    with the login itself; the login never goes anywhere else.
    """
    url = f"https://{ref.host}/v2/{quote(ref.repo, safe='/')}/manifests/{quote(ref.tag)}"
    headers = {"Accept": ACCEPT}
    status, answer, _ = await _head(url, headers, allow_private)
    if status == 401:
        challenge = answer.get("www-authenticate", "")
        if challenge.lower().startswith("basic") and login:
            auth = _basic(login)
        else:
            auth = "Bearer " + await _token(challenge, ref.repo, allow_private, ref.host, login)
        status, answer, _ = await _head(url, {**headers, "Authorization": auth}, allow_private)
    if status == 404:
        raise RegistryError("images.not_found")
    if status in (401, 403):
        raise RegistryError("images.denied")
    digest = answer.get("docker-content-digest", "")
    if status != 200 or not DIGEST.fullmatch(digest):
        raise RegistryError("images.registry_unreachable")
    return digest
