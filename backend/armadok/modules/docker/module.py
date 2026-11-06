"""Declaration of the `docker` module."""

from armadok.core.module import Module, RelationProvider, Right, Signal, WorkerOp
from armadok.modules.docker import prune, relations
from armadok.modules.docker.routes import router

module = Module(
    name="docker",
    requires=("workers",),
    routes=(router,),
    rights=(
        Right("docker.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        Right("docker.control", depends_on=("docker.read",), node_owner=True, level="operate"),
        # Removing a container loses its writable layer: kept apart, and never for API keys.
        Right(
            "docker.remove",
            depends_on=("docker.control",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
        # Logs can hold secrets that applications print.
        Right("docker.logs", depends_on=("docker.read",), node_owner=True, level="operate"),
    ),
    signals=(Signal("docker.changed", "docker.read"),),
    audit_events=("docker.container_action", "docker.logs_read", "docker.prune"),
    hook_actions=(prune.ACTION,),
    relations=(
        RelationProvider(
            name="docker",
            right="docker.read",
            node_types=("docker", "full"),
            provide=relations.provide,
        ),
    ),
    # Only for nodes that run Docker.
    worker_ops=(
        WorkerOp("docker.info", ("docker", "full")),
        WorkerOp("docker.containers.list", ("docker", "full")),
        WorkerOp("docker.images.list", ("docker", "full")),
        WorkerOp("docker.networks.list", ("docker", "full")),
        WorkerOp("docker.volumes.list", ("docker", "full")),
        WorkerOp("docker.container.action", ("docker", "full")),
        WorkerOp("docker.container.logs", ("docker", "full")),
        WorkerOp("docker.prune", ("docker", "full")),
        WorkerOp("docker.stats", ("docker", "full")),
    ),
)
