"""Declaration of the `relations` module (the aggregator; providers live in other modules)."""

from armadok.core.module import Module
from armadok.modules.relations.routes import router

module = Module(name="relations", requires=("nodes",), routes=(router,))
