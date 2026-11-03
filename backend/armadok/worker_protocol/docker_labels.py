"""Which container labels may leave a machine.

Labels can carry secrets (a Traefik `basicauth.users` hash, a forward-auth URL with a token,
custom headers). Only labels the interface needs, to link a container to its project and to
its Traefik router, are allowed; everything else is dropped. The worker filters first and the
hub filters again, so a compromised worker cannot widen what API callers see.
"""

from __future__ import annotations

import re

MAX_VALUE_LENGTH = 500
MAX_LABELS = 100

# Routers (what is published), the service port, and which network Traefik uses. Middleware
# *definitions* are deliberately absent: that is where credentials live. A router only names
# its middlewares, which is all the relations need.
ALLOWED = tuple(
    re.compile(pattern)
    for pattern in (
        r"com\.docker\.compose\.(project|service)",
        r"traefik\.enable",
        r"traefik\.docker\.network",
        r"traefik\.http\.routers\.[A-Za-z0-9_.-]+\.(rule|entrypoints|service|middlewares|priority)",
        r"traefik\.http\.routers\.[A-Za-z0-9_.-]+\.tls(\.certresolver)?",
        r"traefik\.http\.services\.[A-Za-z0-9_.-]+\.loadbalancer\.server\.(port|scheme)",
    )
)


def is_allowed(key: str) -> bool:
    """Return True if a label with this key may be shown."""
    return any(pattern.fullmatch(key) for pattern in ALLOWED)


def filter_labels(labels: dict[str, str]) -> dict[str, str]:
    """Keep allowed labels only, with bounded values and count."""
    kept = {k: str(v)[:MAX_VALUE_LENGTH] for k, v in labels.items() if is_allowed(k)}
    return dict(list(kept.items())[:MAX_LABELS])
