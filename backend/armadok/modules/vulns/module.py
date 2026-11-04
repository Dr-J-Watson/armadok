"""Declaration of the `vulns` module: known vulnerabilities in Armadok's own components."""

from armadok.core.module import Module, NotificationEvent, Right, Signal
from armadok.modules.vulns import repository
from armadok.modules.vulns.routes import router
from armadok.modules.vulns.service import watch_loop

module = Module(
    name="vulns",
    requires=("users",),
    routes=(router,),
    # Sensitive and platform-wide: the list says exactly what could be attacked.
    rights=(
        Right("vulns.read", sensitive=True),
        Right("vulns.manage", depends_on=("vulns.read",), sensitive=True),
    ),
    audit_events=("vulns.scan", "vulns.ignore", "vulns.unignore"),
    signals=(Signal("vulns.changed", "vulns.read", shared=True),),
    notification_events=(
        NotificationEvent(
            "vulns.found",
            "critical",
            "vulns.read",
            {
                "en": (
                    "Vulnerability in Armadok",
                    "{count} new serious vulnerabilities in Armadok's components "
                    "(worst: {severity}, {component}).",
                ),
                "fr": (
                    "Faille dans Armadok",
                    "{count} nouvelles failles sérieuses dans les composants d'Armadok "
                    "(la pire : {severity}, {component}).",
                ),
            },
        ),
    ),
    migrations=(repository.MIGRATION, repository.MISSED_MIGRATION),
    tasks=(watch_loop,),
)
