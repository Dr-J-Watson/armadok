"""What can be set on a node, and what is allowed for each setting.

Two kinds. **Places** (folders and files the worker reads) can only be narrowed inside the root
the worker's environment gives (see `armadok.worker.nodeconfig`): the hub keeps the value and
sends it with every call. **Defaults** (Traefik entry points, certificate resolver) are used by
the interface to fill in forms and are never sent to the worker.
"""

from __future__ import annotations

import re

from armadok.worker.nodeconfig import SETTINGS

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_ENTRYPOINTS = 10

# Which places make sense on which kind of node.
PLACES_BY_TYPE: dict[str, tuple[str, ...]] = {
    "docker": ("projects_dir", "backup_dir"),
    "network": ("traefik_dir", "traefik_static_dir", "acme_file", "accesslog_file"),
    "full": (
        "projects_dir",
        "backup_dir",
        "traefik_dir",
        "traefik_static_dir",
        "acme_file",
        "accesslog_file",
    ),
}
PLACES = frozenset(SETTINGS)
ENTRYPOINTS = "traefik_entrypoints"  # stored as comma-separated names
RESOLVER = "traefik_resolver"


def entrypoints_of(stored: str) -> list[str]:
    """Return the entry point names kept in a stored value."""
    return [n for n in stored.split(",") if n]
