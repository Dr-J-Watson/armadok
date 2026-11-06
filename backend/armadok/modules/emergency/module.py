"""Declaration of the `emergency` module."""

from armadok.core.module import Module, NotificationEvent, Right, Signal
from armadok.modules.emergency import repository
from armadok.modules.emergency.routes import router

module = Module(
    name="emergency",
    requires=("nodes", "users", "roles"),
    routes=(router,),
    rights=(
        # An administrator's way into a node that is not theirs: sensitive, never for keys.
        Right("emergency.use", sensitive=True),
        # The owner's: see and acknowledge the notices about their node. Owner role only.
        Right("emergency.notices", depends_on=("nodes.read",), node_owner=True, level="owner"),
    ),
    notification_events=(
        NotificationEvent(
            name="emergency.started",
            severity="critical",
            right="emergency.notices",
            templates={
                "en": (
                    "Emergency access to {node}",
                    "An administrator opened an emergency session on {node}. Open the node's "
                    "Emergency tab to read why and what was done.",
                ),
                "fr": (
                    "Accès d'urgence à {node}",
                    "Un administrateur a ouvert une session d'urgence sur {node}. Ouvrez "
                    "l'onglet Urgence du nœud pour lire pourquoi et ce qui a été fait.",
                ),
            },
        ),
    ),
    signals=(Signal("emergency.changed", "emergency.notices"),),
    audit_events=(
        "emergency.start",
        "emergency.end",
        "emergency.cut",
        "emergency.acknowledge",
    ),
    migrations=(repository.MIGRATION,),
    session_grant_sources=(repository.session_grants,),
    scope_unblockers=(repository.session_scopes,),
)
