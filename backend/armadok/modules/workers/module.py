"""Declaration of the `workers` module."""

from armadok.core.module import Module, NotificationEvent, Right, Signal, WorkerOp
from armadok.modules.workers import repository
from armadok.modules.workers.routes import on_node_deleted, router

module = Module(
    name="workers",
    node_tables=(("workers", "delete"), ("enrolment_tokens", "delete")),
    requires=("nodes",),
    routes=(router,),
    rights=(
        Right("workers.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        Right("workers.manage", depends_on=("workers.read",), node_owner=True, level="admin"),
    ),
    audit_events=("worker.enrolment_created", "worker.enrolled", "worker.revoked", "worker.ping"),
    listeners=(("node.deleted", on_node_deleted),),
    signals=(
        Signal("workers.changed", "workers.read", shared=True),
        Signal("worker.changed", "workers.read", shared=True),
    ),
    notification_events=(
        NotificationEvent(
            "worker.connected",
            "info",
            "workers.read",
            {
                "en": ("Worker connected", "The worker of {node} is connected."),
                "fr": ("Worker connecté", "Le worker de {node} est connecté."),
            },
            cooldown=60,
        ),
        NotificationEvent(
            "worker.disconnected",
            "warning",
            "workers.read",
            {
                "en": ("Worker disconnected", "The worker of {node} is no longer connected."),
                "fr": ("Worker déconnecté", "Le worker de {node} n'est plus connecté."),
            },
            cooldown=60,
        ),
    ),
    # The closed list of operations the hub may ask a worker to run.
    worker_ops=(WorkerOp("ping", ("network", "docker", "full")),),
    migrations=(repository.MIGRATION,),
)
