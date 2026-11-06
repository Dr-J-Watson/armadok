"""Declaration of the `imagescan` module: known flaws in the images containers run."""

from armadok.core.module import Module, NotificationEvent, Right, Signal, WorkerOp
from armadok.modules.imagescan import repository, scheduled
from armadok.modules.imagescan.routes import router

module = Module(
    name="imagescan",
    requires=("workers", "docker", "vulns"),
    routes=(router,),
    rights=(
        Right("imagescan.read", depends_on=("docker.read",), node_owner=True, level="view"),
        # Not sensitive: it only sends package names and versions to a public database, and only
        # when the platform switch is on. A scan runs no code of the image.
        Right("imagescan.run", depends_on=("imagescan.read",), node_owner=True, level="operate"),
    ),
    hook_actions=(scheduled.ACTION,),
    audit_events=("imagescan.run",),
    signals=(Signal("imagescan.changed", "imagescan.read"),),
    notification_events=(
        NotificationEvent(
            "imagescan.found",
            "warning",
            "imagescan.read",
            {
                "en": (
                    "New serious flaws in an image",
                    "On this node, the image {image} has {count} new serious flaw(s).",
                ),
                "fr": (
                    "Nouvelles failles graves dans une image",
                    "Sur cette node, l'image {image} a {count} nouvelle(s) faille(s) grave(s).",
                ),
            },
        ),
    ),
    worker_ops=(WorkerOp("images.packages", ("docker", "full")),),
    migrations=(repository.MIGRATION,),
)
