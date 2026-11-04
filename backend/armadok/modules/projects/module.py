"""Declaration of the `projects` module."""

from armadok.core.module import Module, NotificationEvent, Right, Signal, WorkerOp
from armadok.modules.projects import hook, tasks
from armadok.modules.projects.deploy import resume_watchers
from armadok.modules.projects.deploy import router as deploy_router
from armadok.modules.projects.routes import router

_DOCKER = ("docker", "full")

module = Module(
    name="projects",
    node_tables=(("deploy_tasks", "delete"),),
    requires=("workers",),
    routes=(router, deploy_router),
    rights=(
        Right("projects.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        Right(
            "projects.files.read", depends_on=("projects.read",), node_owner=True, level="develop"
        ),
        Right(
            "projects.files.write",
            depends_on=("projects.files.read",),
            node_owner=True,
            level="develop",
        ),
        # Running `docker compose` is as powerful as being root on the machine: sensitive, so
        # it is never carried by an API key unless explicitly confirmed, and flagged everywhere.
        Right(
            "projects.deploy",
            depends_on=("projects.read",),
            node_owner=True,
            sensitive=True,
            level="operate",
        ),
        # Logs and task output can hold secrets that applications print.
        Right("projects.logs", depends_on=("projects.read",), node_owner=True, level="operate"),
    ),
    hook_actions=(hook.ACTION, hook.RESTART),
    signals=(
        Signal("projects.changed", "projects.read"),
        Signal("tasks.changed", "projects.read"),
    ),
    audit_events=(
        "project.file_read",
        "project.file_write",
        "project.action",
        "project.action_done",
        "project.logs_read",
    ),
    notification_events=(
        NotificationEvent(
            "deploy.succeeded",
            "info",
            "projects.read",
            {
                "en": ("Deployment succeeded", "{action} of {project} on {node} succeeded."),
                "fr": ("Déploiement réussi", "{action} de {project} sur {node} a réussi."),
            },
        ),
        NotificationEvent(
            "deploy.failed",
            "warning",
            "projects.read",
            {
                "en": ("Deployment failed", "{action} of {project} on {node} failed."),
                "fr": ("Déploiement en échec", "{action} de {project} sur {node} a échoué."),
            },
        ),
    ),
    tasks=(resume_watchers,),
    migrations=(tasks.MIGRATION,),
    worker_ops=(
        WorkerOp("projects.list", _DOCKER),
        WorkerOp("projects.file.read", _DOCKER),
        WorkerOp("projects.file.write", _DOCKER),
        WorkerOp("compose.start", _DOCKER),
        WorkerOp("compose.task", _DOCKER),
        WorkerOp("compose.ps", _DOCKER),
        WorkerOp("compose.logs", _DOCKER),
        WorkerOp("compose.check", _DOCKER),
    ),
)
