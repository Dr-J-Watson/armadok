"""Declaration of the `service_accounts` module."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.service_accounts import repository
from armadok.modules.service_accounts.routes import router

module = Module(
    name="service_accounts",
    requires=("users", "apikeys"),
    routes=(router,),
    # Sensitive: a service account is a lasting way into the platform. Not for API keys.
    rights=(Right("service_accounts.manage", sensitive=True),),
    signals=(Signal("service-accounts.changed", "service_accounts.manage"),),
    audit_events=(
        "service_account.create",
        "service_account.close",
        "service_account.key_create",
        "service_account.key_revoke",
    ),
    migrations=(repository.MIGRATION,),
    grant_sources=(repository.borrowed_grants,),
    account_blockers=(repository.owner_is_inactive,),
)
