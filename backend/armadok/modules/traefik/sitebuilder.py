"""Builds a Traefik site (a router and its service) from a short, validated description.

The text is generated, never taken from the caller: every value is checked against a strict
pattern first (no backtick, quote or newline can get into a rule or a URL), and the YAML is
produced by the YAML library, not by string pasting. The description is kept in the file as a
comment, so the site can be edited from the form again; if someone edits the file by hand,
the generated text no longer matches and the form refuses to overwrite their work.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
SITE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
HOST = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,100}")
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SERVER_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
HEADER = "# Managed by Armadok. Change it with the site form: a manual edit blocks the form.\n"
SPEC_PREFIX = "# armadok-site: "
MAX_SPEC_LINE = 4096  # a real description is a few hundred characters

# A value in a rule sits between backticks: anything but a backtick or a control character is plain
# text there (that is how Traefik reads it), so a value can never end the string and add a rule.
RULE_VALUE = re.compile(r"[^`\x00-\x1f\x7f]{1,200}")

SiteName = Annotated[str, StringConstraints(pattern=SITE_NAME_PATTERN)]
TraefikName = Annotated[str, StringConstraints(pattern=rf"^{NAME}$")]
MiddlewareRef = Annotated[str, StringConstraints(pattern=rf"^{NAME}(@[a-z]{{1,20}})?$")]
# A site may only send to a service of the files (never `api@internal`, which is Traefik's own).
ServiceRef = Annotated[str, StringConstraints(pattern=rf"^{NAME}(@file)?$")]


class Condition(BaseModel):
    """One more condition a request must meet (joined with the hosts by AND)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "PathPrefix", "Path", "PathRegexp", "Method", "Header", "HeaderRegexp", "Query",
        "QueryRegexp", "ClientIP",
    ]  # fmt: skip
    value: str
    # The header or parameter a Header / Query condition looks at.
    name: str | None = None

    @model_validator(mode="after")
    def well_formed(self) -> Condition:
        """Check the value, and that only the conditions that take a name have one."""
        if not RULE_VALUE.fullmatch(self.value):
            raise ValueError("invalid value")
        takes_name = self.type in ("Header", "HeaderRegexp", "Query", "QueryRegexp")
        if takes_name != (self.name is not None) or (
            self.name is not None and not RULE_VALUE.fullmatch(self.name)
        ):
            raise ValueError("invalid name")
        if self.type in ("Path", "PathPrefix") and not self.value.startswith("/"):
            raise ValueError("a path starts with /")
        return self

    def render(self) -> str:
        """Return the matcher as Traefik writes it."""
        if self.name is not None:
            return f"{self.type}(`{self.name}`, `{self.value}`)"
        return f"{self.type}(`{self.value}`)"


# Fields added after the first release: left out of the stored description while they hold their
# default, so a file written before them is still recognised as untouched.
_LATER_DEFAULTS: dict = {"priority": None, "servers": [], "service": None, "conditions": []}


class SiteSpec(BaseModel):
    """What a site is: where it answers, what is applied, where requests go."""

    model_config = ConfigDict(extra="forbid")
    name: SiteName
    hosts: list[str] = Field(min_length=1, max_length=10)
    path_prefix: str | None = Field(default=None, max_length=101)
    container: str | None = Field(default=None, max_length=128)
    url: str | None = Field(default=None, max_length=300)
    port: int | None = Field(default=None, ge=1, le=65535)
    scheme: Literal["http", "https"] = "http"
    middlewares: list[MiddlewareRef] = Field(default_factory=list, max_length=20)
    https: bool = True
    cert_resolver: TraefikName | None = None
    entrypoints: list[TraefikName] | None = Field(default=None, max_length=5)
    priority: int | None = Field(default=None, ge=1, le=1_000_000)
    # More upstreams for the same site (the load is shared between them).
    servers: list[Annotated[str, StringConstraints(max_length=300)]] = Field(
        default_factory=list, max_length=10
    )
    # An existing service to send to, instead of making one.
    service: ServiceRef | None = None
    conditions: list[Condition] = Field(default_factory=list, max_length=10)

    @field_validator("hosts")
    @classmethod
    def valid_hosts(cls, hosts: list[str]) -> list[str]:
        """Lower-case host names, each a plain DNS name (so a rule cannot be injected)."""
        cleaned = [h.strip().lower() for h in hosts]
        if not all(HOST.fullmatch(h) for h in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("invalid host")
        return cleaned

    @field_validator("path_prefix")
    @classmethod
    def valid_path(cls, value: str | None) -> str | None:
        """Accept an absolute path made of safe characters only."""
        if value in (None, ""):
            return None
        if not PATH.fullmatch(value):
            raise ValueError("invalid path")
        return value

    @model_validator(mode="after")
    def one_target(self) -> SiteSpec:
        """Exactly one target: a container with a port, a plain server address, or a service."""
        if self.service is not None:
            if self.container or self.url or self.port or self.servers:
                raise ValueError("a service replaces the other targets")
        elif (self.container is None) == (self.url is None):
            raise ValueError("give a container, an address or a service")
        elif self.container is not None:
            if not CONTAINER.fullmatch(self.container) or self.port is None:
                raise ValueError("invalid container target")
        else:
            self.server_address()  # raises when unusable
        for extra in self.servers:
            _upstream(extra)
        if self.cert_resolver and not self.https:
            raise ValueError("a certificate resolver needs HTTPS")
        return self

    def server_address(self) -> str:
        """Return the upstream as `scheme://host:port` (no credentials, path or query)."""
        if self.container is not None:
            return f"{self.scheme}://{self.container}:{self.port}"
        assert self.url is not None
        return _upstream(self.url)

    def rule(self) -> str:
        """Return the Traefik rule: any of the hosts, and every extra condition."""
        hosts = " || ".join(f"Host(`{h}`)" for h in self.hosts)
        more = ([f"PathPrefix(`{self.path_prefix}`)"] if self.path_prefix else []) + [
            c.render() for c in self.conditions
        ]
        if len(self.hosts) > 1 and more:
            hosts = f"({hosts})"
        return " && ".join([hosts, *more])


def _upstream(url: str) -> str:
    """Reduce an address to `scheme://host[:port]`, or raise when it is not a plain server."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("invalid address") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or not SERVER_HOST.fullmatch(parts.hostname)
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("invalid address")
    return f"{parts.scheme}://{parts.hostname}" + (f":{port}" if port else "")


def render(spec: SiteSpec) -> str:
    """Return the file text for a site."""
    router: dict = {
        "rule": spec.rule(),
        "entryPoints": spec.entrypoints or (["websecure"] if spec.https else ["web"]),
        "service": spec.service or spec.name,
    }
    if spec.priority is not None:
        router["priority"] = spec.priority
    if spec.middlewares:
        router["middlewares"] = list(spec.middlewares)
    if spec.https:
        router["tls"] = {"certResolver": spec.cert_resolver} if spec.cert_resolver else {}
    http: dict = {"routers": {spec.name: router}}
    if spec.service is None:
        upstreams = [spec.server_address(), *(_upstream(u) for u in spec.servers)]
        http["services"] = {
            spec.name: {"loadBalancer": {"servers": [{"url": u} for u in upstreams]}}
        }
    document = {"http": http}
    body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=False)
    stored = {k: v for k, v in spec.model_dump().items() if _LATER_DEFAULTS.get(k, object()) != v}
    line = json.dumps(stored, sort_keys=True, separators=(",", ":"))
    return f"{HEADER}{SPEC_PREFIX}{line}\n{body}"


def spec_of(text: str) -> SiteSpec | None:
    """Return the description stored in a generated file, or None for any other file."""
    for line in text.splitlines()[:3]:
        if line.startswith(SPEC_PREFIX):
            if len(line) > MAX_SPEC_LINE:
                return None
            try:
                return SiteSpec.model_validate(json.loads(line[len(SPEC_PREFIX) :]))
            except (ValueError, RecursionError):  # also: absurdly nested JSON
                return None
    return None


def untouched(text: str) -> SiteSpec | None:
    """Return the description only if the file is exactly what the form would write now."""
    spec = spec_of(text)
    return spec if spec is not None and render(spec) == text else None
