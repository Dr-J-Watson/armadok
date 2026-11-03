"""Declaration of the `roles` module: roles, groups and who can do what."""

from armadok.core.module import Module, Signal
from armadok.modules.roles import repository
from armadok.modules.roles.routes import router


def _sync(conn, settings, registry) -> None:
    """Keep the stored built-in roles in step with the rights the modules declare."""
    repository.sync_builtin(conn, registry.rights())


module = Module(
    name="roles",
    requires=("users", "nodes"),
    routes=(router,),
    signals=(
        Signal("roles.changed", "users.read", shared=True),
        Signal("groups.changed", "users.read", shared=True),
        Signal("assignments.changed", "users.read", shared=True),
    ),
    audit_events=(
        "role.create",
        "role.update",
        "role.delete",
        "group.create",
        "group.delete",
        "group.member_add",
        "group.member_remove",
        "assignment.add",
        "assignment.remove",
    ),
    startup=(_sync,),
)
