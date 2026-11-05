"""Declaration of the `git` module: projects that come from, and follow, a Git repository."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.git import repository
from armadok.modules.git.routes import router

_DOCKER = ("docker", "full")

module = Module(
    name="git",
    requires=("workers", "projects"),
    routes=(router,),
    rights=(
        # Holds access tokens for private repositories: sensitive, so never for API keys.
        Right(
            "git.credentials",
            depends_on=("projects.files.write",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    audit_events=(
        "git.login_save",
        "git.login_delete",
        "git.clone",
        "git.update",
        "git.forget",
    ),
    worker_ops=(
        WorkerOp("git.clone", _DOCKER),
        WorkerOp("git.status", _DOCKER),
        WorkerOp("git.pull", _DOCKER),
    ),
    migrations=(repository.MIGRATION,),
)
