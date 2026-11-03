"""Declaration of the `workers` module."""

from armadok.core.module import Module, Right
from armadok.modules.workers import repository
from armadok.modules.workers.routes import on_node_deleted, router

module = Module(
    name="workers",
    requires=("nodes",),
    routes=(router,),
    rights=(
        Right("workers.read", depends_on=("nodes.read",), node_owner=True),
        Right("workers.manage", depends_on=("workers.read",), node_owner=True),
    ),
    audit_events=("worker.enrolment_created", "worker.enrolled", "worker.revoked", "worker.ping"),
    listeners=(("node.deleted", on_node_deleted),),
    signals=("workers.changed",),
    # The closed list of operations the hub may ask a worker to run.
    worker_ops=("ping",),
    migrations=(repository.MIGRATION,),
)
