"""Reminders: tell an account when a note's reminder date has come.

The notification carries only where the note is (object type and id), never its text: notes are
private, and the message may travel to a webhook or a phone.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from armadok.core.events import emit

log = logging.getLogger("armadok.notes")
CHECK_EVERY_SECONDS = 60


async def send_due_reminders(app: Any) -> int:
    """Notify the owner of every note whose reminder is due and not done; return how many."""
    now = datetime.now(UTC).isoformat()
    conn = app.state.settings.open_db()
    try:
        due = conn.execute(
            "SELECT id, owner_id, node_id, object_type, object_id FROM notes"
            " WHERE remind_at IS NOT NULL AND remind_at <= ? AND done = 0 AND reminded_at IS NULL"
            " LIMIT 200",
            (now,),
        ).fetchall()
        for note in due:
            # Marked first: a failing delivery must not make the same reminder fire every minute.
            conn.execute("UPDATE notes SET reminded_at = ? WHERE id = ?", (now, note["id"]))
            conn.commit()
            try:
                await emit(
                    app,
                    "notify",
                    event="note.reminder",
                    node_id=note["node_id"],
                    user_ids=[note["owner_id"]],
                    params={"where": f"{note['object_type']} {note['object_id']}"[:100]},
                )
            except Exception:  # noqa: BLE001
                log.exception("could not send a note reminder")
        return len(due)
    finally:
        conn.close()


async def reminder_loop(app: Any) -> None:
    """Check for due reminders until the application stops."""
    while True:
        await asyncio.sleep(CHECK_EVERY_SECONDS)
        try:
            await send_due_reminders(app)
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("reminder check failed")
