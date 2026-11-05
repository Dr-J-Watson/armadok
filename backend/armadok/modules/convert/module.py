"""Declaration of the `convert` module: converters for what people bring from elsewhere."""

from armadok.core.module import Module
from armadok.modules.convert.routes import router

module = Module(name="convert", routes=(router,))
