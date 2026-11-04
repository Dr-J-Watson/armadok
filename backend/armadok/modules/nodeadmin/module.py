"""Declaration of the `nodeadmin` module: the quick actions of a platform administrator."""

from armadok.core.module import Module, NotificationEvent, Right, Signal
from armadok.modules.nodeadmin.actions import router as actions_router
from armadok.modules.nodeadmin.purge import purge_loop
from armadok.modules.nodeadmin.routes import router

module = Module(
    name="nodeadmin",
    requires=("nodes", "users", "roles", "service_accounts"),
    routes=(router, actions_router),
    tasks=(purge_loop,),
    # Sensitive and platform-wide: it never gives access to a node's content.
    rights=(Right("nodes.admin", sensitive=True),),
    signals=(Signal("admin.changed", "nodes.admin", shared=True),),
    audit_events=(
        "node.suspend",
        "node.resume",
        "node.restore",
        "node.disconnect",
        "node.admin_delete",
        "node.purge",
        "node.takeover",
        "node.owner_ban",
        "node.creation_removed",
    ),
    notification_events=(
        NotificationEvent(
            "nodeadmin.owner_changed",
            "warning",
            None,
            {
                "en": (
                    "Machine ownership changed",
                    "An administrator changed the owner of {node}.",
                ),
                "fr": (
                    "Propriété d'une machine modifiée",
                    "Un administrateur a changé le propriétaire de {node}.",
                ),
            },
        ),
        NotificationEvent(
            "nodeadmin.deleted",
            "warning",
            None,
            {
                "en": ("Machine deleted", "An administrator deleted {node}. It can be restored."),
                "fr": (
                    "Machine supprimée",
                    "Un administrateur a supprimé {node}. Elle peut être restaurée.",
                ),
            },
        ),
        NotificationEvent(
            "nodeadmin.disconnected",
            "warning",
            None,
            {
                "en": ("Worker cut", "An administrator cut the worker of {node}."),
                "fr": ("Worker coupé", "Un administrateur a coupé le worker de {node}."),
            },
        ),
    ),
)
