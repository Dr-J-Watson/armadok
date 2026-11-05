"""Declaration of the `terminal` module: a shell in a throwaway container, on one project."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.terminal import repository
from armadok.modules.terminal.routes import router

_DOCKER = ("docker", "full")

module = Module(
    name="terminal",
    requires=("workers", "projects", "users"),
    routes=(router,),
    rights=(
        # A shell is as powerful as its container allows (and the person may write to the
        # project's files from it): sensitive, so never for API keys.
        Right(
            "projects.terminal",
            depends_on=("projects.files.write",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    audit_events=("terminal.settings", "terminal.open", "terminal.close"),
    worker_ops=(
        WorkerOp("terminal.open", _DOCKER),
        WorkerOp("terminal.read", _DOCKER),
        WorkerOp("terminal.write", _DOCKER),
        WorkerOp("terminal.resize", _DOCKER),
        WorkerOp("terminal.close", _DOCKER),
    ),
    migrations=(repository.MIGRATION,),
)
