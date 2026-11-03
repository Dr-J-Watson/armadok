"""Declaration of the `docker` module."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.docker.routes import router

module = Module(
    name="docker",
    requires=("workers",),
    routes=(router,),
    rights=(Right("docker.read", depends_on=("nodes.read",), node_owner=True),),
    # Only for machines that run Docker.
    worker_ops=(
        WorkerOp("docker.info", ("docker", "full")),
        WorkerOp("docker.containers.list", ("docker", "full")),
    ),
)
