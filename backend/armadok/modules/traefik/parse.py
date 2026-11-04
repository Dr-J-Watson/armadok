"""Reads Traefik dynamic configuration (YAML) into routers, services and middlewares.

Safe by construction: only the plain YAML loader is used, aliases and anchors are refused (a
small file could otherwise expand into gigabytes), and every value is type-checked, since the
files can come from anywhere. Names follow Traefik's own: `name@file`.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

import yaml

PROVIDER = "file"
MAX_ITEMS = 500
MAX_TEXT = 500
_HOST = re.compile(r"Host\(([^)]*)\)")
_BACKTICKED = re.compile(r"`([^`]+)`")


class YamlError(ValueError):
    """Raised when a file is not acceptable YAML."""


class _NoAliasLoader(yaml.SafeLoader):
    """Plain YAML without aliases (`*name`) and without dates.

    Aliases allow exponential expansion. Dates are dropped because `2001-02-30` looks like one
    and then fails to build; Traefik configuration has no use for them, so such values simply
    stay text.
    """

    yaml_implicit_resolvers = {
        first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed")
        return super().compose_node(parent, index)


def load(text: str) -> Any:
    """Parse YAML text or raise `YamlError`."""
    try:
        return yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506  (safe loader subclass)
    # ValueError / OverflowError: forced tags such as `!!int x`, or numbers too long to convert.
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError):
        raise YamlError("invalid YAML") from None


def qualify(name: str, provider: str = PROVIDER) -> str:
    """Return Traefik's full name for a reference (`auth` -> `auth@file`)."""
    return name if "@" in name else f"{name}@{provider}"


def _text(value: Any) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v[:MAX_TEXT] for v in value if isinstance(v, str)][:50]


def hosts_of(rule: str) -> list[str]:
    """Return the host names a router rule matches (`Host(`a.example.com`)`)."""
    return [h for group in _HOST.findall(rule) for h in _BACKTICKED.findall(group)]


def safe_server(url: str) -> str:
    """Reduce a server URL to `scheme://host[:port]`.

    Credentials, paths and query strings (which may hold tokens) are dropped: this is shown to
    people who may read the sites but not the files.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    return f"{parts.scheme}://{host}{port}" if parts.scheme and host else ""


_EMPTY_ERROR = {"error": "invalid_yaml", "routers": [], "services": [], "middlewares": []}
_CACHE: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_CACHE_SIZE = 256


def summarize(path: str, text: str, sha256: str | None = None) -> dict[str, Any]:
    """Return the HTTP routers, services and middlewares of one file.

    A file that is not valid YAML (or has an unexpected shape) gives `error` instead of
    failing the whole view. With `sha256`, the result is remembered: parsing is slow Python and
    the same files are read again on every refresh.
    """
    key = (path, sha256) if sha256 else None
    if key and key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    try:
        result = _summarize(path, text)
    except (YamlError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        result = {"path": path, **_EMPTY_ERROR}
    if key:
        _CACHE[key] = result
        if len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return result


def _summarize(path: str, text: str) -> dict[str, Any]:
    """Read one file's routers, services and middlewares (may raise on odd shapes)."""
    document = load(text)
    http = document.get("http") if isinstance(document, dict) else None
    http = http if isinstance(http, dict) else {}

    def section(key: str) -> dict[str, Any]:
        value = http.get(key)
        return value if isinstance(value, dict) else {}

    routers = []
    for name, spec in list(section("routers").items())[:MAX_ITEMS]:
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        tls = spec.get("tls")
        routers.append(
            {
                "name": name,
                "id": qualify(name),
                "rule": _text(spec.get("rule")),
                "service": qualify(_text(spec.get("service"))) if spec.get("service") else "",
                "entrypoints": _names(spec.get("entryPoints")),
                "middlewares": [qualify(m) for m in _names(spec.get("middlewares"))],
                "tls": tls is not None and tls is not False,
                "cert_resolver": _text(tls.get("certResolver")) if isinstance(tls, dict) else "",
                "path": path,
            }
        )
    services = []
    for name, spec in list(section("services").items())[:MAX_ITEMS]:
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        balancer = spec.get("loadBalancer")
        servers = balancer.get("servers") if isinstance(balancer, dict) else None
        urls = [
            clean
            for s in (servers if isinstance(servers, list) else [])
            if isinstance(s, dict) and isinstance(s.get("url"), str)
            if (clean := safe_server(s["url"]))
        ][:20]
        services.append({"name": name, "id": qualify(name), "urls": urls, "path": path})
    middlewares = []
    for name, spec in list(section("middlewares").items())[:MAX_ITEMS]:
        if not isinstance(name, str) or not isinstance(spec, dict) or not spec:
            continue
        kind = next((k for k in spec if isinstance(k, str)), "")
        chain = spec.get("chain")
        # A chain names other middlewares: the check follows these links.
        members = chain.get("middlewares") if isinstance(chain, dict) else None
        middlewares.append(
            {
                "name": name,
                "id": qualify(name),
                "type": kind[:50],
                "path": path,
                "refs": [qualify(m) for m in _names(members)],
            }
        )
    return {
        "path": path,
        "error": None,
        "routers": routers,
        "services": services,
        "middlewares": middlewares,
    }
