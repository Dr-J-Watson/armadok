"""Declaration of the `nodes` module: what it contributes to the application."""

from armadok.core.module import Module, Right
from armadok.modules.nodes import repository
from armadok.modules.nodes.routes import router

module = Module(
    name="nodes",
    routes=(router,),
    rights=(
        Right("nodes.read"),
        Right("nodes.create", depends_on=("nodes.read",)),
        Right("nodes.update", depends_on=("nodes.read",)),
        Right("nodes.delete", depends_on=("nodes.read",)),
    ),
    audit_events=("node.create", "node.update", "node.delete"),
    signals=("nodes.changed",),
    migrations=(repository.MIGRATION,),
)
