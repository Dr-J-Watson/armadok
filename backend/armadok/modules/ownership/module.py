"""Declaration of the `ownership` module: owners of the objects inside a node."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.ownership import repository, service
from armadok.modules.ownership.routes import router

module = Module(
    name="ownership",
    requires=("nodes", "users"),
    routes=(router,),
    rights=(
        # Sensitive and owner-only: a gift or a seizure changes who answers for an object.
        Right(service.GIVE, depends_on=("nodes.read",), sensitive=True, node_owner=True),
        Right(
            service.MANAGE,
            depends_on=("nodes.read",),
            sensitive=True,
            node_owner=True,
            level="owner",
        ),
    ),
    audit_events=(
        "ownership.offer",
        "ownership.accept",
        "ownership.refuse",
        "ownership.withdraw",
        "ownership.take",
        "ownership.release",
    ),
    signals=(Signal("ownership.changed", "nodes.read", shared=True),),
    migrations=(repository.MIGRATION,),
    node_tables=(("object_owners", "delete"), ("ownership_offers", "delete")),
    registry_grant_sources=(service.grant_source,),
    access_listeners=(service.release_lost,),
    tasks=(service.watch_loop,),
)
