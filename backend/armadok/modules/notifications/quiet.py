"""Do-not-disturb hours: no message on the phone at night, unless it is critical.

Only outside channels (a webhook, ntfy, Discord, e-mail) are held back; the inbox always gets
the entry, so nothing is lost. A critical event ignores the quiet hours: that is the point of
the word.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from armadok.core.db import Migration

MIGRATION = Migration(
    "notifications-0003-quiet-hours",
    """
    CREATE TABLE notification_quiet (
        user_id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        start TEXT NOT NULL,
        end TEXT NOT NULL,
        timezone TEXT NOT NULL
    );
    """,
)


def parse(value: Any) -> time:
    """Read `HH:MM`; raise `ValueError` for anything else."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("time")
    hour, minute = value[:2], value[3:]
    if not (hour.isdigit() and minute.isdigit() and hour.isascii() and minute.isascii()):
        raise ValueError("time")
    return time(int(hour), int(minute))


def zone(name: str) -> ZoneInfo:
    """Return the time zone or raise `ValueError`."""
    if not isinstance(name, str) or len(name) > 64:
        raise ValueError("timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("timezone") from None


def get(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Return the account's quiet hours (switched off by default)."""
    row = conn.execute("SELECT * FROM notification_quiet WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {"enabled": False, "start": "22:00", "end": "07:00", "timezone": ""}
    return {
        "enabled": bool(row["enabled"]),
        "start": row["start"],
        "end": row["end"],
        "timezone": row["timezone"],
    }


def put(conn: sqlite3.Connection, user_id: str, setting: dict[str, Any]) -> None:
    """Store the (already validated) quiet hours."""
    conn.execute(
        "INSERT INTO notification_quiet (user_id, enabled, start, end, timezone)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled,"
        " start = excluded.start, end = excluded.end, timezone = excluded.timezone",
        (user_id, int(setting["enabled"]), setting["start"], setting["end"], setting["timezone"]),
    )
    conn.commit()


def is_quiet(setting: dict[str, Any], now: datetime | None = None) -> bool:
    """Return True if `now` falls in the quiet hours (a window may cross midnight)."""
    if not setting["enabled"]:
        return False
    try:
        local = (now or datetime.now(UTC)).astimezone(zone(setting["timezone"])).time()
        start, end = parse(setting["start"]), parse(setting["end"])
    except ValueError:
        return False  # a broken setting never silences anything
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end
