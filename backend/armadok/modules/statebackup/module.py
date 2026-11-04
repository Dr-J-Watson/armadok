"""Declaration of the `statebackup` module: backups of the hub's own database and key."""

from armadok.core.module import Module, NotificationEvent, Right, Signal
from armadok.modules.statebackup.routes import router
from armadok.modules.statebackup.watch import watch_loop

module = Module(
    name="statebackup",
    requires=("users",),
    routes=(router,),
    # Sensitive and platform-wide: a backup holds every account and every secret (encrypted).
    rights=(Right("state_backup.manage", sensitive=True),),
    audit_events=(
        "statebackup.create",
        "statebackup.delete",
        "statebackup.download",
        "statebackup.key_generated",
    ),
    signals=(Signal("statebackups.changed", "state_backup.manage", shared=True),),
    notification_events=(
        NotificationEvent(
            "statebackup.failed",
            "critical",
            "state_backup.manage",
            {
                "en": (
                    "Armadok backup failed",
                    "The automatic backup of Armadok's data failed ({code}).",
                ),
                "fr": (
                    "Sauvegarde d'Armadok en échec",
                    "La sauvegarde automatique des données d'Armadok a échoué ({code}).",
                ),
            },
            cooldown=6 * 3600,
        ),
    ),
    tasks=(watch_loop,),
)
