"""Declaration of the `users` module: accounts, first-start setup and sessions."""

from armadok.core.module import Module, Right
from armadok.modules.users import repository, setup
from armadok.modules.users.auth import authenticate
from armadok.modules.users.routes_auth import router as auth_router
from armadok.modules.users.routes_users import router as users_router

module = Module(
    name="users",
    routes=(auth_router, users_router),
    rights=(
        Right("users.read"),
        Right("users.create", depends_on=("users.read",)),
        Right("users.update", depends_on=("users.read",)),
        Right("users.reset_password", depends_on=("users.update",)),
        Right("grants.manage", depends_on=("users.read",)),
    ),
    audit_events=(
        "setup.complete",
        "auth.login",
        "auth.logout",
        "user.create",
        "user.update",
        "grant.add",
        "grant.remove",
    ),
    migrations=(repository.MIGRATION, repository.THROTTLE_MIGRATION),
    authenticator=authenticate,
    startup=(setup.ensure_install_code,),
)
