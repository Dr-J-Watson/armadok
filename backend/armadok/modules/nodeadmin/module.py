"""Declaration of the `nodeadmin` module: suspend, resume and restore nodes."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.nodeadmin.routes import router

module = Module(
    name="nodeadmin",
    requires=("nodes", "users", "roles"),
    routes=(router,),
    # Sensitive and platform-wide: it never gives access to a node's content.
    rights=(Right("nodes.admin", sensitive=True),),
    signals=(Signal("admin.changed", "nodes.admin", shared=True),),
    audit_events=("node.suspend", "node.resume", "node.restore"),
)
