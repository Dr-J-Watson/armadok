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

SiteName = Annotated[str, StringConstraints(pattern=SITE_NAME_PATTERN)]
TraefikName = Annotated[str, StringConstraints(pattern=rf"^{NAME}$")]
MiddlewareRef = Annotated[str, StringConstraints(pattern=rf"^{NAME}(@[a-z]{{1,20}})?$")]


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
        """Exactly one target: a container with a port, or a plain server address."""
        if (self.container is None) == (self.url is None):
            raise ValueError("give a container or an address")
        if self.container is not None:
            if not CONTAINER.fullmatch(self.container) or self.port is None:
                raise ValueError("invalid container target")
        else:
            self.server_address()  # raises when unusable
        if self.cert_resolver and not self.https:
            raise ValueError("a certificate resolver needs HTTPS")
        return self

    def server_address(self) -> str:
        """Return the upstream as `scheme://host:port` (no credentials, path or query)."""
        if self.container is not None:
            return f"{self.scheme}://{self.container}:{self.port}"
        assert self.url is not None
        try:
            parts = urlsplit(self.url)
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

    def rule(self) -> str:
        """Return the Traefik rule: any of the hosts, below the path prefix if there is one."""
        hosts = " || ".join(f"Host(`{h}`)" for h in self.hosts)
        if len(self.hosts) > 1 and self.path_prefix:
            hosts = f"({hosts})"
        return hosts + (f" && PathPrefix(`{self.path_prefix}`)" if self.path_prefix else "")


def render(spec: SiteSpec) -> str:
    """Return the file text for a site."""
    router: dict = {
        "rule": spec.rule(),
        "entryPoints": spec.entrypoints or (["websecure"] if spec.https else ["web"]),
        "service": spec.name,
    }
    if spec.middlewares:
        router["middlewares"] = list(spec.middlewares)
    if spec.https:
        router["tls"] = {"certResolver": spec.cert_resolver} if spec.cert_resolver else {}
    document = {
        "http": {
            "routers": {spec.name: router},
            "services": {
                spec.name: {"loadBalancer": {"servers": [{"url": spec.server_address()}]}}
            },
        }
    }
    body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=False)
    line = json.dumps(spec.model_dump(), sort_keys=True, separators=(",", ":"))
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
