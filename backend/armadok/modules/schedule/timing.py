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

    Three kinds: `interval` (every N minutes), `daily` (at HH:MM in a time zone, on the chosen
    weekdays, 0 = Monday; none chosen means every day) and `cron` (the usual five-field
    expression, in a time zone, never more often than every 15 minutes).
    """
    kind = spec.get("kind")
    if kind == "cron":
        expr, zone = spec.get("expr"), spec.get("timezone", "UTC")
        if not isinstance(expr, str) or len(expr) > 100 or not isinstance(zone, str):
            raise ValueError("expr")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("timezone") from None
        fields = parse_cron(expr)
        normalised = {"kind": "cron", "expr": " ".join(expr.split()), "timezone": zone}
        # Seen over the coming runs, not just the next two: "0,5 * * * *" is fine only if the gap
        # between every pair is long enough.
        now, previous = datetime.now(UTC), None
        for _ in range(30):
            current = _next_cron(fields, ZoneInfo(zone), previous or now)
            if previous is not None and current - previous < timedelta(minutes=MIN_EVERY_MINUTES):
                raise ValueError("too_frequent")
            previous = current
        return normalised
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


_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_DAYS = "sun mon tue wed thu fri sat".split()


def _cron_field(text: str, low: int, high: int, names: list[str] | None, base: int) -> set[int]:
    """Expand one cron field (`*`, `*/n`, `a-b`, `a-b/n`, lists, names) into its set of values."""

    def number(part: str) -> int:
        if names and part.lower() in names:
            return names.index(part.lower()) + base
        if not part.isascii() or not part.isdigit():
            raise ValueError("expr")
        return int(part)

    values: set[int] = set()
    for chunk in text.split(","):
        body, _, step_text = chunk.partition("/")
        if step_text and (not step_text.isascii() or not step_text.isdigit() or int(step_text) < 1):
            raise ValueError("expr")
        step = int(step_text) if step_text else 1
        if body == "*":
            start, end = low, high
        elif "-" in body:
            first, _, last = body.partition("-")
            start, end = number(first), number(last)
        else:
            start = number(body)
            end = high if step_text else start
        if not low <= start <= end <= high:
            raise ValueError("expr")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Read a five-field cron expression; raise `ValueError` if it is not valid.

    Returns the minutes, hours, days of the month, months and days of the week (0 = Monday, as
    Python numbers them) plus whether the day-of-month and day-of-week fields were left open.
    As in classic cron, when both are restricted a day matches if either of them does.
    """
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("expr")
    minutes = _cron_field(parts[0], 0, 59, None, 0)
    hours = _cron_field(parts[1], 0, 23, None, 0)
    doms = _cron_field(parts[2], 1, 31, None, 1)
    months = _cron_field(parts[3], 1, 12, _MONTHS, 1)
    # Cron counts Sunday as 0 (or 7); Python counts Monday as 0.
    raw = _cron_field(parts[4], 0, 7, _DAYS, 0)
    dows = {(d - 1) % 7 for d in raw}
    return minutes, hours, doms, months, dows, parts[2] == "*", parts[4] == "*"


def _next_cron(fields: Any, zone: ZoneInfo, after: datetime) -> datetime:
    """Return the first matching minute strictly after `after`, in UTC."""
    minutes, hours, doms, months, dows, dom_open, dow_open = fields
    local = after.astimezone(zone)
    for offset in range(366 * 5):
        day = (local + timedelta(days=offset)).date()
        if day.month not in months:
            continue
        by_dom, by_dow = day.day in doms, day.weekday() in dows
        if dom_open or dow_open:
            matches = by_dom and by_dow
        else:
            matches = by_dom or by_dow
        if not matches:
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                if candidate.astimezone(UTC) > after:
                    return candidate.astimezone(UTC)
    raise ValueError("no run found")  # an impossible date such as 31 February


def next_run(spec: dict[str, Any], after: datetime) -> datetime:
    """Return the first run strictly after `after` (an aware datetime), in UTC."""
    if spec["kind"] == "cron":
        return _next_cron(parse_cron(spec["expr"]), ZoneInfo(spec["timezone"]), after)
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
