"""Declaration of the `notes` module."""

from armadok.core.module import Module, NotificationEvent, Right
from armadok.modules.notes import repository
from armadok.modules.notes.reminders import reminder_loop
from armadok.modules.notes.routes import router

module = Module(
    name="notes",
    requires=("nodes",),
    routes=(router,),
    # Everyone may keep private notes: it gives access to nothing but their own memos.
    rights=(Right("notes.write", baseline=True),),
    migrations=(repository.MIGRATION, repository.REMINDER_MIGRATION),
    notification_events=(
        NotificationEvent(
            "note.reminder",
            "info",
            None,  # sent to the note's owner only, never by rights
            {
                "en": ("Note reminder", "You asked to be reminded about {where}."),
                "fr": ("Rappel de note", "Vous avez demandé un rappel pour {where}."),
            },
        ),
    ),
    tasks=(reminder_loop,),
)
