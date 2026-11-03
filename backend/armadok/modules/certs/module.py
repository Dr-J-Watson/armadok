"""Declaration of the `certs` module."""

from armadok.core.module import Module, NotificationEvent, RelationProvider, Right, WorkerOp
from armadok.modules.certs import relations
from armadok.modules.certs.routes import router
from armadok.modules.certs.watch import watch_loop

_NODE_TYPES = ("network", "full")

module = Module(
    name="certs",
    requires=("workers", "relations"),
    routes=(router,),
    rights=(
        # See the certificates that cover hosts you can see.
        Right("certs.read", depends_on=("nodes.read",), node_owner=True),
        # See every certificate of the node (and be warned about any of them).
        Right("certs.read_all", depends_on=("certs.read",), node_owner=True),
    ),
    notification_events=(
        NotificationEvent(
            "cert.expiring",
            "warning",
            "certs.read_all",
            {
                "en": ("Certificate expiring", "On {node}: {names} expires in {days} days."),
                "fr": (
                    "Certificat bientôt expiré",
                    "Sur {node} : {names} expire dans {days} jours.",
                ),
            },
            cooldown=24 * 3600,
        ),
        NotificationEvent(
            "cert.expired",
            "critical",
            "certs.read_all",
            {
                "en": ("Certificate expired", "On {node}: {names} has expired."),
                "fr": ("Certificat expiré", "Sur {node} : {names} a expiré."),
            },
            cooldown=24 * 3600,
        ),
    ),
    relations=(
        RelationProvider(
            name="certs", right="certs.read_all", node_types=_NODE_TYPES, provide=relations.provide
        ),
    ),
    worker_ops=(WorkerOp("certs.list", _NODE_TYPES),),
    tasks=(watch_loop,),
)
