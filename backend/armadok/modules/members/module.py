"""Declaration of the `members` module: node members and transfer of ownership."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.members.routes import router

module = Module(
    name="members",
    requires=("nodes", "roles", "users"),
    routes=(router,),
    rights=(
        # Who may invite, change and remove people on a node. Given to its owner.
        Right("nodes.members", depends_on=("nodes.read",), node_owner=True, level="admin"),
        # Only the owner role carries it: handing the node over cannot be delegated.
        Right("nodes.transfer", depends_on=("nodes.read",), node_owner=True, level="owner"),
    ),
    signals=(
        Signal("members.changed", "nodes.members"),
        Signal("transfer.changed", "nodes.read"),
    ),
    audit_events=("node.member_add", "node.member_change", "node.member_remove", "node.transfer"),
)
