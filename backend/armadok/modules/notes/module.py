"""Declaration of the `notes` module."""

from armadok.core.module import Module, Right
from armadok.modules.notes import repository
from armadok.modules.notes.routes import router

module = Module(
    name="notes",
    requires=("nodes",),
    routes=(router,),
    # Everyone may keep private notes: it gives access to nothing but their own memos.
    rights=(Right("notes.write", baseline=True),),
    migrations=(repository.MIGRATION,),
)
