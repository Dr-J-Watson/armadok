"""Declaration of the `notifications` module."""

from armadok.core.module import Module, Right, Signal
from armadok.modules.notifications import platform, push_keys, quiet, repository
from armadok.modules.notifications.routes import router
from armadok.modules.notifications.service import handle_notify

module = Module(
    name="notifications",
    node_tables=(("notifications", "delete"), ("notification_rules", "delete")),
    requires=("users", "nodes"),
    signals=(Signal("notifications.changed", "notifications.use"),),
    routes=(router,),
    # Everyone has an inbox, channels and rules: they only ever concern their own account.
    rights=(
        Right("notifications.use", baseline=True),
        # Platform switches (the e-mail channel): it changes what every account can do.
        Right("notifications.admin", sensitive=True),
    ),
    migrations=(
        repository.MIGRATION,
        repository.INDEX_MIGRATION,
        quiet.MIGRATION,
        repository.CONFIRM_MIGRATION,
        platform.MIGRATION,
        push_keys.MIGRATION,
    ),
    audit_events=(
        "notification.channel_create",
        "notification.channel_confirm",
        "notification.channel_delete",
        "notification.channel_test",
        "notification.quiet_set",
        "notification.platform_set",
    ),
    # Other modules raise notifications with `emit(app, "notify", event=..., params=...)`.
    listeners=(("notify", handle_notify),),
)
