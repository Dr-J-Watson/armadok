"""Declaration of the `diagnostics` module."""

from armadok.core.module import Module
from armadok.modules.diagnostics.routes import router

module = Module(name="diagnostics", requires=("nodes", "workers"), routes=(router,))
