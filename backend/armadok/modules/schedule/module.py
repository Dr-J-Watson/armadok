"""Declaration of the `schedule` module: scheduled tasks (restart, redeploy, prune...)."""

from armadok.core.module import Module, NotificationEvent, Right, Signal
from armadok.modules.schedule import repository
from armadok.modules.schedule.routes import router
from armadok.modules.schedule.runner import watch_loop

module = Module(
    name="schedule",
    requires=("workers", "hooks"),
    routes=(router,),
    rights=(
        Right("schedule.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        # Sensitive: a task acts later, unattended, with its creator's rights. Not for API keys.
        Right(
            "schedule.manage",
            depends_on=("schedule.read",),
            sensitive=True,
            node_owner=True,
            level="admin",
        ),
    ),
    audit_events=(
        "schedule.create",
        "schedule.update",
        "schedule.delete",
        "schedule.run",
        "schedule.run_now",
    ),
    signals=(Signal("schedule.changed", "schedule.read"),),
    notification_events=(
        NotificationEvent(
            "schedule.failed",
            "warning",
            "schedule.read",
            {
                "en": ("Scheduled task failed", 'On {node}: "{name}" did not run ({code}).'),
                "fr": (
                    "Tâche planifiée en échec",
                    "Sur {node} : « {name} » n'a pas pu s'exécuter ({code}).",
                ),
            },
            cooldown=6 * 3600,
        ),
    ),
    migrations=(repository.MIGRATION,),
    tasks=(watch_loop,),
)
