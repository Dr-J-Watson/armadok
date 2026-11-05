"""Declaration of the `autoupdate` module: projects that update themselves on a schedule."""

from armadok.core.module import Module
from armadok.modules.autoupdate import repository, scheduled

module = Module(
    name="autoupdate",
    requires=("schedule", "projects", "git", "imageupdates", "docker"),
    hook_actions=(scheduled.ACTION,),
    migrations=(repository.MIGRATION,),
)
