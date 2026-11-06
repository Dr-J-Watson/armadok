"""Declaration of the `machine` module: resources of the node a node stands for."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.machine.routes import router

module = Module(
    name="machine",
    requires=("workers",),
    routes=(router,),
    rights=(Right("machine.read", depends_on=("nodes.read",), node_owner=True, level="view"),),
    worker_ops=(WorkerOp("machine.stats", ("network", "docker", "full")),),
)
