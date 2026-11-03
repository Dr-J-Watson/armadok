"""When a scheduled task runs next. Pure functions, no I/O."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_EVERY_MINUTES = 15
MAX_EVERY_MINUTES = 7 * 24 * 60
TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


def validate(spec: dict[str, Any]) -> dict[str, Any]:
    """Check and normalise a schedule; raise `ValueError` if it makes no sense.

    Two kinds: `interval` (every N minutes) and `daily` (at HH:MM in a time zone, on the chosen
    weekdays, 0 = Monday; none chosen means every day).
    """
    kind = spec.get("kind")
    if kind == "interval":
        every = spec.get("every_minutes")
        if type(every) is not int or not MIN_EVERY_MINUTES <= every <= MAX_EVERY_MINUTES:
            raise ValueError("every_minutes")
        return {"kind": "interval", "every_minutes": every}
    if kind == "daily":
        at, zone, days = spec.get("at"), spec.get("timezone", "UTC"), spec.get("weekdays", [])
        if not isinstance(at, str) or not TIME.fullmatch(at):
            raise ValueError("at")
        if not isinstance(zone, str) or len(zone) > 64:
            raise ValueError("timezone")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("timezone") from None
        if (
            not isinstance(days, list)
            or len(days) > 7
            or any(type(d) is not int or not 0 <= d <= 6 for d in days)
        ):
            raise ValueError("weekdays")
        return {"kind": "daily", "at": at, "timezone": zone, "weekdays": sorted(set(days))}
    raise ValueError("kind")


def next_run(spec: dict[str, Any], after: datetime) -> datetime:
    """Return the first run strictly after `after` (an aware datetime), in UTC."""
    if spec["kind"] == "interval":
        return after.astimezone(UTC) + timedelta(minutes=spec["every_minutes"])
    zone = ZoneInfo(spec["timezone"])
    hour, minute = (int(x) for x in spec["at"].split(":"))
    local = after.astimezone(zone)
    # Eight days always contain a matching day, even with a single weekday chosen.
    for offset in range(9):
        day = (local + timedelta(days=offset)).date()
        if spec["weekdays"] and day.weekday() not in spec["weekdays"]:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        if candidate.astimezone(UTC) > after:
            return candidate.astimezone(UTC)
    raise ValueError("no run found")  # unreachable for a validated spec
