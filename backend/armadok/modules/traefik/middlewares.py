"""Builds Traefik middlewares from an intention ("only my network", "ask for a password"...).

Like sites, the text is generated and never taken from the caller: every value is checked
against a strict pattern first (no backtick, quote or newline can get into a header or an
address) and the YAML comes from the YAML library, not from string pasting. The description is
kept in the file as a comment so the form can edit it again; a hand-edited file no longer
matches what the form would write, and the form then refuses to overwrite the work.

Passwords: a password is turned into a bcrypt hash here and only the hash is ever written or
kept. The form never receives a password back; leaving the field empty keeps the old hash.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Annotated, Literal

import bcrypt
import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

HEADER = (
    "# Managed by Armadok. Change it with the middleware form: a manual edit blocks the form.\n"
)
SPEC_PREFIX = "# armadok-middleware: "
MAX_SPEC_LINE = 8192
FOLDER = "middlewares"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
BCRYPT_COST = 12
BCRYPT_HASH = re.compile(r"\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}")
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,100}")
HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
CSP = re.compile(r"[A-Za-z0-9 ;:'\-./*_=]{1,1000}")
MAX_PASSWORD_BYTES = 72  # what bcrypt uses; longer ones are refused, not silently cut
# Addresses of a private network: the default of "only my network".
PRIVATE_RANGES = [
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
]

Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]


def _cidr(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        raise ValueError("invalid address range") from None


class LocalOnly(BaseModel):
    """Only these networks may reach the site (a private network by default)."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["local_only"] = "local_only"
    name: Name
    ranges: list[str] = Field(
        default_factory=lambda: list(PRIVATE_RANGES), min_length=1, max_length=50
    )

    @field_validator("ranges")
    @classmethod
    def valid_ranges(cls, value: list[str]) -> list[str]:
        """Each entry must be an address or a network."""
        return [_cidr(v) for v in value]


class BasicUser(BaseModel):
    """A user of a password-protected site. `password` is used once, never stored or returned."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=f"^{USERNAME.pattern}$")
    password: str | None = Field(
        default=None, min_length=8, max_length=200, exclude=True, repr=False
    )
    hash: str | None = Field(default=None, pattern=f"^{BCRYPT_HASH.pattern}$")


class BasicAuth(BaseModel):
    """Ask for a user name and password."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["basic_auth"] = "basic_auth"
    name: Name
    users: list[BasicUser] = Field(min_length=1, max_length=50)
    remove_header: bool = True

    @field_validator("users")
    @classmethod
    def distinct(cls, value: list[BasicUser]) -> list[BasicUser]:
        """Refuse the same user name twice."""
        if len({u.username for u in value}) != len(value):
            raise ValueError("duplicate user")
        return value


class SecurityHeaders(BaseModel):
    """Common protective response headers."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["security_headers"] = "security_headers"
    name: Name
    hsts_seconds: int = Field(default=31536000, ge=0, le=63072000)
    hsts_subdomains: bool = False
    frame_deny: bool = True
    nosniff: bool = True
    referrer_policy: Literal[
        "no-referrer", "same-origin", "strict-origin", "strict-origin-when-cross-origin"
    ] = "strict-origin-when-cross-origin"
    csp: str | None = Field(default=None, max_length=1000)

    @field_validator("csp")
    @classmethod
    def valid_csp(cls, value: str | None) -> str | None:
        """Accept a content security policy of plain characters only."""
        if value is not None and not CSP.fullmatch(value):
            raise ValueError("invalid policy")
        return value


class RedirectHttps(BaseModel):
    """Send visitors of http:// to https://."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["redirect_https"] = "redirect_https"
    name: Name
    permanent: bool = True


class StripPrefix(BaseModel):
    """Remove a path prefix before the request reaches the application."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["strip_prefix"] = "strip_prefix"
    name: Name
    prefixes: list[str] = Field(min_length=1, max_length=10)

    @field_validator("prefixes")
    @classmethod
    def valid_prefixes(cls, value: list[str]) -> list[str]:
        """Each prefix is a plain path."""
        for prefix in value:
            if not PATH.fullmatch(prefix) or prefix == "/":
                raise ValueError("invalid prefix")
        return value


class RateLimit(BaseModel):
    """Limit the number of requests per second (and the size of a burst)."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["rate_limit"] = "rate_limit"
    name: Name
    average: int = Field(default=100, ge=1, le=100000)
    burst: int = Field(default=50, ge=1, le=100000)
    period_seconds: int = Field(default=1, ge=1, le=3600)


class Compress(BaseModel):
    """Compress responses (gzip, brotli, zstd as the visitor accepts)."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["compress"] = "compress"
    name: Name
    min_bytes: int = Field(default=1024, ge=0, le=10_000_000)


class ForwardAuth(BaseModel):
    """Ask another service (a single sign-on) whether the request may pass."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["forward_auth"] = "forward_auth"
    name: Name
    address: str = Field(max_length=300)
    trust_forward_header: bool = False
    response_headers: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        """Accept a plain http(s) address: no credentials, no query, no fragment."""
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ValueError("invalid address") from None
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", parts.hostname)
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not PATH.fullmatch(parts.path or "/")
        ):
            raise ValueError("invalid address")
        return (
            f"{parts.scheme}://{parts.hostname}" + (f":{port}" if port else "") + (parts.path or "")
        )

    @field_validator("response_headers")
    @classmethod
    def valid_headers(cls, value: list[str]) -> list[str]:
        """Header names, nothing else."""
        if not all(HEADER_NAME.fullmatch(v) for v in value):
            raise ValueError("invalid header name")
        return value


Spec = Annotated[
    LocalOnly
    | BasicAuth
    | SecurityHeaders
    | RedirectHttps
    | StripPrefix
    | RateLimit
    | Compress
    | ForwardAuth,
    Field(discriminator="intent"),
]


class SpecBody(BaseModel):
    """Body of a creation or preview."""

    model_config = ConfigDict(extra="forbid")
    spec: Spec


class SpecUpdate(SpecBody):
    """Body of an update: the description and the version being replaced."""

    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def hash_password(password: str) -> str:
    """Return the bcrypt hash of a password (refusing one bcrypt would cut)."""
    raw = password.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("password too long")
    return bcrypt.hashpw(raw, bcrypt.gensalt(BCRYPT_COST)).decode().replace("$2b$", "$2y$", 1)


MAX_NEW_PASSWORDS = 10  # hashing is slow on purpose: a request may not ask for dozens at once


def check_passwords(spec: Spec, previous: Spec | None = None) -> int:
    """Validate everything about the passwords before any hashing; return how many are new.

    Raises `ValueError` ("needed", "too long" or "too many") so that no time is spent hashing
    for a request that will be refused anyway.
    """
    if not isinstance(spec, BasicAuth):
        return 0
    old = {u.username: u.hash for u in previous.users} if isinstance(previous, BasicAuth) else {}
    fresh = 0
    for user in spec.users:
        if user.password:
            if len(user.password.encode()) > MAX_PASSWORD_BYTES:
                raise ValueError("password too long")
            fresh += 1
        elif not (user.hash or old.get(user.username)):
            raise ValueError("a password is needed for a new user")
    if fresh > MAX_NEW_PASSWORDS:
        raise ValueError("too many new passwords at once")
    return fresh


def resolve(spec: Spec, previous: Spec | None = None) -> Spec:
    """Turn passwords into hashes (keeping the old hash of a user left without a new password).

    Returns a copy in which no password remains. This is slow when passwords are given (a
    bcrypt hash takes a fraction of a second): call it off the event loop.
    """
    if not isinstance(spec, BasicAuth):
        return spec
    check_passwords(spec, previous)
    old = {u.username: u.hash for u in previous.users} if isinstance(previous, BasicAuth) else {}
    users = [
        BasicUser(
            username=u.username,
            hash=hash_password(u.password) if u.password else (u.hash or old.get(u.username)),
        )
        for u in spec.users
    ]
    return spec.model_copy(update={"users": users})


def _body(spec: Spec) -> dict:
    """Return the Traefik middleware definition for a (resolved) description."""
    if isinstance(spec, LocalOnly):
        return {"ipAllowList": {"sourceRange": list(spec.ranges)}}
    if isinstance(spec, BasicAuth):
        return {
            "basicAuth": {
                "users": [f"{u.username}:{u.hash}" for u in spec.users],
                "removeHeader": spec.remove_header,
            }
        }
    if isinstance(spec, SecurityHeaders):
        headers: dict = {"referrerPolicy": spec.referrer_policy}
        if spec.hsts_seconds:
            headers["stsSeconds"] = spec.hsts_seconds
            headers["stsIncludeSubdomains"] = spec.hsts_subdomains
        if spec.frame_deny:
            headers["frameDeny"] = True
        if spec.nosniff:
            headers["contentTypeNosniff"] = True
        if spec.csp:
            headers["contentSecurityPolicy"] = spec.csp
        return {"headers": headers}
    if isinstance(spec, RedirectHttps):
        return {"redirectScheme": {"scheme": "https", "permanent": spec.permanent}}
    if isinstance(spec, StripPrefix):
        return {"stripPrefix": {"prefixes": list(spec.prefixes)}}
    if isinstance(spec, RateLimit):
        return {
            "rateLimit": {
                "average": spec.average,
                "burst": spec.burst,
                "period": f"{spec.period_seconds}s",
            }
        }
    if isinstance(spec, Compress):
        return {"compress": {"minResponseBodyBytes": spec.min_bytes}}
    forward: dict = {"address": spec.address, "trustForwardHeader": spec.trust_forward_header}
    if spec.response_headers:
        forward["authResponseHeaders"] = list(spec.response_headers)
    return {"forwardAuth": forward}


def render(spec: Spec) -> str:
    """Return the file text for a resolved description (no password in it, only hashes)."""
    if isinstance(spec, BasicAuth) and not all(u.hash for u in spec.users):
        raise ValueError("a user has no password hash")
    document = {"http": {"middlewares": {spec.name: _body(spec)}}}
    body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=False)
    line = json.dumps(spec.model_dump(), sort_keys=True, separators=(",", ":"))
    return f"{HEADER}{SPEC_PREFIX}{line}\n{body}"


def reference(name: str) -> str:
    """Return the name to put in a router's `middlewares` list."""
    return f"{name}@file"


def spec_of(text: str) -> Spec | None:
    """Return the description stored in a generated file, or None for any other file."""
    adapter = SpecBody
    for line in text.splitlines()[:3]:
        if line.startswith(SPEC_PREFIX):
            if len(line) > MAX_SPEC_LINE:
                return None
            try:
                return adapter.model_validate({"spec": json.loads(line[len(SPEC_PREFIX) :])}).spec
            except (ValueError, RecursionError):
                return None
    return None


def untouched(text: str) -> Spec | None:
    """Return the description only if the file is exactly what the form would write now."""
    spec = spec_of(text)
    return spec if spec is not None and render(spec) == text else None
