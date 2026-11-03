"""Declaration of the `projects` module."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.projects.routes import router

module = Module(
    name="projects",
    requires=("workers",),
    routes=(router,),
    rights=(
        Right("projects.read", depends_on=("nodes.read",), node_owner=True),
        Right("projects.files.read", depends_on=("projects.read",), node_owner=True),
        Right("projects.files.write", depends_on=("projects.files.read",), node_owner=True),
    ),
    audit_events=("project.file_read", "project.file_write"),
    worker_ops=(
        WorkerOp("projects.list", ("docker", "full")),
        WorkerOp("projects.file.read", ("docker", "full")),
        WorkerOp("projects.file.write", ("docker", "full")),
    ),
)
