"""Declaration of the `templates` module: Compose texts kept to start new projects from."""

from armadok.core.module import Module, Right
from armadok.modules.templates import repository
from armadok.modules.templates.routes import router

module = Module(
    name="templates",
    requires=("users",),
    routes=(router,),
    # Everyone may keep their own templates: they give access to nothing.
    rights=(Right("templates.use", baseline=True),),
    audit_events=("templates.create", "templates.delete"),
    migrations=(repository.MIGRATION,),
)
