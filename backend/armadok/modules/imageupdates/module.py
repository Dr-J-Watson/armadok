"""Declaration of the `imageupdates` module: is a newer version of a running image out."""

from armadok.core.module import Module, NotificationEvent, Right, Signal, WorkerOp
from armadok.modules.imageupdates import repository, scheduled
from armadok.modules.imageupdates.routes import router

module = Module(
    name="imageupdates",
    requires=("workers", "docker"),
    routes=(router,),
    rights=(
        Right("imageupdates.read", depends_on=("docker.read",), node_owner=True, level="view"),
        # Not sensitive: it sends only public image names and tags to the registries they name.
        Right(
            "imageupdates.check",
            depends_on=("imageupdates.read",),
            node_owner=True,
            level="operate",
        ),
        # Holds registry passwords: sensitive, so never for API keys.
        Right(
            "imageupdates.credentials",
            depends_on=("imageupdates.check",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    hook_actions=(scheduled.ACTION,),
    audit_events=("imageupdates.check", "imageupdates.login_save", "imageupdates.login_delete"),
    signals=(Signal("image-updates.changed", "imageupdates.read"),),
    notification_events=(
        NotificationEvent(
            "images.update",
            "info",
            "imageupdates.read",
            {
                "en": ("New image version", "A newer version of {image} is available."),
                "fr": (
                    "Nouvelle version d'image",
                    "Une version plus récente de {image} est disponible.",
                ),
            },
        ),
    ),
    worker_ops=(WorkerOp("docker.images.digests", ("docker", "full")),),
    migrations=(repository.MIGRATION, repository.LOGINS_MIGRATION),
)
