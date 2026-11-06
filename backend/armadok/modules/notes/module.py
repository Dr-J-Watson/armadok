"""Declaration of the `notes` module."""

from armadok.core.module import Module, NotificationEvent, Right, SearchProvider, Signal
from armadok.modules.notes import repository
from armadok.modules.notes import search as _search
from armadok.modules.notes.reminders import reminder_loop
from armadok.modules.notes.routes import router

module = Module(
    name="notes",
    # Notes about a node that was erased for good go with it (their shares follow).
    node_tables=(("notes", "delete"),),
    search=(SearchProvider("notes", _search.run),),
    requires=("nodes", "users"),
    signals=(Signal("notes.changed", "notes.write"),),
    routes=(router,),
    # Everyone may keep private notes: it gives access to nothing but their own memos.
    rights=(Right("notes.write", baseline=True),),
    migrations=(repository.MIGRATION, repository.REMINDER_MIGRATION, repository.SHARES_MIGRATION),
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
    ownership_listeners=(repository.hand_over,),
)
