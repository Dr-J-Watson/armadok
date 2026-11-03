"""Declaration of the `users` module: accounts, first-start setup and sessions."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.users import repository, setup
from armadok.modules.users.auth import authenticate
from armadok.modules.users.routes_2fa import router as twofa_router
from armadok.modules.users.routes_auth import router as auth_router
from armadok.modules.users.routes_users import catalogue_router
from armadok.modules.users.routes_users import router as users_router

module = Module(
    name="users",
    routes=(auth_router, twofa_router, users_router, catalogue_router),
    rights=(
        Right("users.read"),
        Right("users.create", depends_on=("users.read",)),
        Right("users.update", depends_on=("users.read",)),
        Right("users.reset_password", depends_on=("users.update",), sensitive=True),
        Right("users.reset_2fa", depends_on=("users.update",), sensitive=True),
        Right("grants.manage", depends_on=("users.read",), sensitive=True),
    ),
    signals=(Signal("users.changed", "users.read", shared=True),),
    audit_events=(
        "setup.complete",
        "auth.login",
        "auth.logout",
        "auth.2fa.enable",
        "auth.2fa.disable",
        "user.create",
        "user.update",
        "user.2fa_reset",
        "grant.add",
        "grant.remove",
    ),
    migrations=(
        repository.MIGRATION,
        repository.THROTTLE_MIGRATION,
        repository.TOTP_MIGRATION,
        repository.LANGUAGE_MIGRATION,
        repository.KIND_MIGRATION,
    ),
    authenticator=authenticate,
    startup=(setup.ensure_install_code,),
)
