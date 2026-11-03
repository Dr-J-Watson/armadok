"""Declaration of the `uptime` module: availability probes of sites and services."""

from armadok.core.module import Module, NotificationEvent, Right, Signal, WorkerOp
from armadok.modules.uptime import repository
from armadok.modules.uptime.routes import router
from armadok.modules.uptime.watch import watch_loop

_NODE_TYPES = ("network", "full")

module = Module(
    name="uptime",
    requires=("workers",),
    routes=(router,),
    rights=(
        Right("uptime.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        # Sensitive: a probe makes the machine send a request to an address of the creator's
        # choice. Not for API keys.
        Right(
            "uptime.manage",
            depends_on=("uptime.read",),
            sensitive=True,
            node_owner=True,
            level="admin",
        ),
    ),
    audit_events=("uptime.create", "uptime.update", "uptime.delete", "uptime.check"),
    signals=(Signal("uptime.changed", "uptime.read"),),
    notification_events=(
        NotificationEvent(
            "uptime.down",
            "critical",
            "uptime.read",
            {
                "en": ("Site unavailable", "On {node}: {name} does not respond ({reason})."),
                "fr": ("Site indisponible", "Sur {node} : {name} ne répond pas ({reason})."),
            },
        ),
        NotificationEvent(
            "uptime.up",
            "info",
            "uptime.read",
            {
                "en": ("Site back", "On {node}: {name} responds again."),
                "fr": ("Site rétabli", "Sur {node} : {name} répond de nouveau."),
            },
        ),
    ),
    worker_ops=(WorkerOp("uptime.tcp", _NODE_TYPES), WorkerOp("uptime.http", _NODE_TYPES)),
    migrations=(repository.MIGRATION, repository.RECOVERY),
    tasks=(watch_loop,),
)
