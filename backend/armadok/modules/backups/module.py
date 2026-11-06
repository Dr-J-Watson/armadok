"""Declaration of the `backups` module: backups and restores of Docker volumes."""

from armadok.core.module import Module, NotificationEvent, Right, Signal, WorkerOp
from armadok.modules.backups import repository, scheduled
from armadok.modules.backups.routes import router
from armadok.modules.backups.watch import watch_loop

_NODE_TYPES = ("docker", "full")

module = Module(
    name="backups",
    requires=("workers", "docker"),
    routes=(router,),
    rights=(
        Right("backups.read", depends_on=("docker.read",), node_owner=True, level="view"),
        Right("backups.create", depends_on=("backups.read",), node_owner=True, level="operate"),
        # Sensitive: a restore overwrites data and a delete loses a backup. Not for API keys.
        Right(
            "backups.manage",
            depends_on=("backups.create",),
            sensitive=True,
            node_owner=True,
            level="admin",
        ),
        # Sensitive: it hands the whole content of a volume out of the node. Not for API keys.
        Right(
            "backups.download",
            depends_on=("backups.read",),
            sensitive=True,
            node_owner=True,
            level="admin",
        ),
    ),
    audit_events=("backup.start", "backup.done", "backup.delete", "backup.download"),
    signals=(Signal("backups.changed", "backups.read"),),
    hook_actions=(scheduled.ACTION,),
    notification_events=(
        NotificationEvent(
            "backup.done",
            "info",
            "backups.read",
            {
                "en": ("Backup job finished", "On {node}: the job on volume {volume} succeeded."),
                "fr": (
                    "Opération de sauvegarde terminée",
                    "Sur {node} : l'opération sur le volume {volume} a réussi.",
                ),
            },
        ),
        NotificationEvent(
            "backup.failed",
            "warning",
            "backups.read",
            {
                "en": (
                    "Backup job failed",
                    "On {node}: the job on volume {volume} failed ({code}).",
                ),
                "fr": (
                    "Opération de sauvegarde en échec",
                    "Sur {node} : l'opération sur le volume {volume} a échoué ({code}).",
                ),
            },
        ),
    ),
    worker_ops=tuple(
        WorkerOp(name, _NODE_TYPES)
        for name in (
            "backups.start",
            "backups.restore",
            "backups.task",
            "backups.list",
            "backups.delete",
            "backups.read",
        )
    ),
    migrations=(repository.MIGRATION, repository.ACTOR),
    tasks=(watch_loop,),
)
