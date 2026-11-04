"""Declaration of the `nodes` module: what it contributes to the application."""

from armadok.core.module import Module, Right, SearchProvider, Signal
from armadok.modules.nodes import local, repository
from armadok.modules.nodes import search as _search
from armadok.modules.nodes.routes import router

module = Module(
    name="nodes",
    search=(SearchProvider("nodes", _search.run),),
    requires=("users",),
    routes=(router,),
    rights=(
        Right("nodes.read", node_owner=True, level="view"),
        # Creating a node must not require reading other people's nodes, hence no dependency.
        Right("nodes.create"),
        Right("nodes.update", depends_on=("nodes.read",), node_owner=True, level="admin"),
        Right("nodes.delete", depends_on=("nodes.read",), node_owner=True, level="owner"),
    ),
    audit_events=("node.create", "node.update", "node.delete"),
    signals=(Signal("nodes.changed", "nodes.read", shared=True),),
    migrations=(repository.MIGRATION, repository.LOCAL_MIGRATION, repository.SUSPEND_MIGRATION),
    scope_blockers=(repository.suspended_scopes,),
    startup=(local.ensure_local_node,),
    listeners=(("setup.completed", local.on_setup_completed),),
)
