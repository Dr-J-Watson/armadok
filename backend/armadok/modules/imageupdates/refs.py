"""Reading an image reference (`nginx:1.27`, `ghcr.io/org/app:v2`) into registry, repository, tag.

The reference comes from a node, so it is untrusted: every part is checked against a strict
pattern before it is put into a URL, and anything unusual is refused (the image is then simply
not followed). References pinned by digest (`@sha256:`) never change, so there is nothing to
follow either.
"""

from __future__ import annotations

import re
from typing import NamedTuple

DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?")
_REPO = re.compile(r"[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*){0,6}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


class Ref(NamedTuple):
    """A parsed reference: where the registry is, the repository inside it, and the tag."""

    host: str  # the host to ask (Docker Hub's API host for docker.io)
    repo: str
    tag: str


def parse(reference: str) -> Ref | None:
    """Return the parts of an image reference, or None if it cannot be followed."""
    if not reference or "@" in reference or len(reference) > 255:
        return None
    name, tag = reference, "latest"
    last = reference.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = reference.rsplit(":", 1)
    first, _, rest = name.partition("/")
    # A first part holding a dot or a colon (or "localhost") names a registry; else Docker Hub.
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, repo = first, rest
    else:
        host, repo = DOCKER_HUB, name
    if host == DOCKER_HUB:
        host = DOCKER_HUB_API
        if "/" not in repo:
            repo = f"library/{repo}"
    if not (_HOST.fullmatch(host) and _REPO.fullmatch(repo) and _TAG.fullmatch(tag)):
        return None
    return Ref(host, repo, tag)


def login_host(text: str) -> str | None:
    """Return the registry host a login is kept under (Docker Hub's names map to its API host)."""
    host = text.strip().lower()
    if host in (DOCKER_HUB, "index.docker.io", "hub.docker.com", DOCKER_HUB_API):
        return DOCKER_HUB_API
    ok = _HOST.fullmatch(host) and ("." in host or host.startswith("localhost"))
    return host if ok else None
