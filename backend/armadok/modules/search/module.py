"""Declaration of the `search` module."""

from armadok.core.module import Module
from armadok.modules.search.routes import router

module = Module(name="search", requires=("users",), routes=(router,))
