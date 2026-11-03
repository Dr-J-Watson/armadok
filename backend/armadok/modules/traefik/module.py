"""Declaration of the `traefik` module."""

from armadok.core.module import Module, RelationProvider, Right, WorkerOp
from armadok.modules.traefik import relations
from armadok.modules.traefik.routes import router

_NODE_TYPES = ("network", "full")

module = Module(
    name="traefik",
    requires=("workers",),
    routes=(router,),
    rights=(
        Right("traefik.read", depends_on=("nodes.read",), node_owner=True),
        Right("traefik.files.read", depends_on=("traefik.read",), node_owner=True),
        Right("traefik.files.write", depends_on=("traefik.files.read",), node_owner=True),
    ),
    audit_events=("traefik.file_read", "traefik.file_write"),
    relations=(
        RelationProvider(
            name="traefik", right="traefik.read", node_types=_NODE_TYPES, provide=relations.provide
        ),
    ),
    worker_ops=(
        WorkerOp("traefik.files.list", _NODE_TYPES),
        WorkerOp("traefik.file.read", _NODE_TYPES),
        WorkerOp("traefik.file.write", _NODE_TYPES),
        WorkerOp("traefik.snapshot", _NODE_TYPES),
    ),
)
