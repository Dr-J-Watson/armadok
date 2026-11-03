"""The single list of enabled modules.

Adding a feature = create its folder under `armadok/modules/` and add its name here.
"""

ENABLED_MODULES: tuple[str, ...] = (
    "users",
    "apikeys",
    "nodes",
    "workers",
    "docker",
    "projects",
    "roles",
    "traefik",
    "audit",
    "notes",
    "notifications",
    "relations",
    "dns",
    "certs",
    "realtime",
    "hooks",
    "metrics",
)
