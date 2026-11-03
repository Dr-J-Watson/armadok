"""Declaration of the `audit` module."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.audit.routes import router

module = Module(
    name="audit",
    requires=("users", "nodes"),
    routes=(router,),
    # Node scope: the owner of a node sees what happened on it. Everywhere: platform audit.
    rights=(Right("audit.read", node_owner=True, level="audit"),),
    signals=(Signal("audit.changed", "audit.read", shared=True),),
    audit_events=("audit.export",),
)
