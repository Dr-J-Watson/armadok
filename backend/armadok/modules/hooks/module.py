"""Declaration of the `hooks` module: incoming hooks with a minimal scope."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.hooks import repository
from armadok.modules.hooks.routes import router

module = Module(
    name="hooks",
    requires=("users", "nodes"),
    routes=(router,),
    # Sensitive: a hook lets a tool deploy without a key. Not for API keys.
    rights=(Right("hooks.manage", sensitive=True),),
    audit_events=("hook.create", "hook.revoke", "hook.fire"),
    signals=(Signal("hooks.changed", "hooks.manage"),),
    migrations=(repository.MIGRATION,),
)
