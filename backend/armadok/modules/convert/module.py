"""Declaration of the `convert` module: converters for what people bring from elsewhere."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.convert.routes import node_router, router

module = Module(
    name="convert",
    requires=("workers",),
    routes=(router, node_router),
    rights=(
        # Reading how containers were started reveals their environment variables, so this is
        # its own sensitive right, never for API keys.
        Right(
            "convert.containers",
            depends_on=("docker.read",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    audit_events=("convert.containers",),
    worker_ops=(WorkerOp("docker.containers.inspect", ("docker", "full")),),
)
