"""Declaration of the `apikeys` module."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.apikeys import repository
from armadok.modules.apikeys.auth import authenticate
from armadok.modules.apikeys.oauth import router as oauth_router
from armadok.modules.apikeys.routes import router

module = Module(
    name="apikeys",
    requires=("users",),
    routes=(router, oauth_router),
    # Baseline: every account may manage its own keys (a key can only carry rights the
    # account already holds, so this grants nothing by itself).
    rights=(Right("apikeys.own", baseline=True),),
    signals=(Signal("apikeys.changed", "apikeys.own"),),
    audit_events=("apikey.create", "apikey.revoke", "oauth.token", "oauth.refused"),
    migrations=(repository.MIGRATION, repository.OAUTH_MIGRATION),
    authenticator=authenticate,
)
