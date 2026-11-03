"""Declaration of the `metrics` module: Prometheus metrics with a dedicated right."""

from armadok.core.module import Module, Right
from armadok.modules.metrics.routes import router

module = Module(
    name="metrics",
    requires=("nodes", "workers"),
    routes=(router,),
    # Not sensitive: it is meant to be given to a key that a monitoring tool holds.
    rights=(Right("metrics.read"),),
)
