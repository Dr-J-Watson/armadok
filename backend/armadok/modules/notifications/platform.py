"""Platform-wide switches of the notification system, set by an administrator.

Today there is one: the e-mail channel. Turning it off stops every mailbox at once (nothing is
sent, new mailboxes are refused) without deleting what people configured, so turning it on again
restores it. It is on by default.
"""

from __future__ import annotations

import sqlite3

from armadok.core.db import Migration

MIGRATION = Migration(
    "notifications-0005-platform-switches",
    """
    CREATE TABLE notification_platform (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
)

EMAIL = "email_enabled"


def email_enabled(conn: sqlite3.Connection) -> bool:
    """Tell whether the e-mail channel is on (it is, unless an administrator turned it off)."""
    row = conn.execute("SELECT value FROM notification_platform WHERE key = ?", (EMAIL,)).fetchone()
    return row is None or row[0] == "1"


def set_email_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    """Turn the e-mail channel on or off for everybody."""
    conn.execute(
        "INSERT INTO notification_platform (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (EMAIL, "1" if enabled else "0"),
    )
    conn.commit()
