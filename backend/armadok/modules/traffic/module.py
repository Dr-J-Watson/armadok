"""Declaration of the `traffic` module: volumes, errors and response times of the sites."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.traffic import repository
from armadok.modules.traffic.routes import router
from armadok.modules.traffic.service import watch_loop

_NODE_TYPES = ("network", "full")

module = Module(
    name="traffic",
    requires=("workers",),
    routes=(router,),
    rights=(
        # Aggregate figures only (no visitor address in Traefik's metrics).
        Right("traffic.read", depends_on=("nodes.read",), node_owner=True, level="view"),
    ),
    worker_ops=(WorkerOp("traffic.scrape", _NODE_TYPES), WorkerOp("traffic.status", _NODE_TYPES)),
    migrations=(repository.MIGRATION,),
    tasks=(watch_loop,),
)
