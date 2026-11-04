"""Declaration of the `status` module: public status pages for probes."""

from armadok.core.module import Module
from armadok.modules.status import repository
from armadok.modules.status.routes import manage_router, public_router

module = Module(
    name="status",
    requires=("uptime",),
    routes=(manage_router, public_router),
    audit_events=("status.publish", "status.unpublish"),
    migrations=(repository.MIGRATION,),
)
