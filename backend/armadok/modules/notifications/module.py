"""Declaration of the `notifications` module."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.notifications import repository
from armadok.modules.notifications.routes import router
from armadok.modules.notifications.service import handle_notify

module = Module(
    name="notifications",
    requires=("users", "nodes"),
    signals=(Signal("notifications.changed", "notifications.use"),),
    routes=(router,),
    # Everyone has an inbox, channels and rules: they only ever concern their own account.
    rights=(Right("notifications.use", baseline=True),),
    migrations=(repository.MIGRATION, repository.INDEX_MIGRATION),
    # Other modules raise notifications with `emit(app, "notify", event=..., params=...)`.
    listeners=(("notify", handle_notify),),
)
