"""Declaration of the `realtime` module: live change signals for open screens."""

from typing import Any

from armadok.core.module import Module
from armadok.modules.realtime.routes import get_hub, router


async def _on_signal(
    app: Any, signal: str, node_id: str | None, actor_id: str | None = None
) -> None:
    """Pass a signal to the connected screens (the hub decides who may receive it)."""
    definition = app.state.registry.signals()[signal]
    get_hub(app).publish(signal, node_id, actor_id, definition.shared)


module = Module(
    name="realtime",
    requires=("users", "nodes"),
    routes=(router,),
    listeners=(("signal", _on_signal),),
)
