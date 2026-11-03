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
    "relations",
)
