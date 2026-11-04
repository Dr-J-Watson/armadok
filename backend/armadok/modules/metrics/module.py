"""Declaration of the `metrics` module: Prometheus metrics with a dedicated right."""

from armadok.core.module import Module, Right
from armadok.modules.metrics.routes import root_router, router

module = Module(
    name="metrics",
    requires=("nodes", "workers"),
    routes=(router,),
    root_routes=(root_router,),
    # Not sensitive: it is meant to be given to a key that a monitoring tool holds.
    rights=(Right("metrics.read"),),
)
