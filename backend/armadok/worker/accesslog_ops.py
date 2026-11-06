"""Reading Traefik's JSON access log, on the node that runs it.

The log is a file the operator mounts read-only into the worker and names in
`ARMADOK_TRAEFIK_ACCESSLOG`; the hub never names a path. Each call returns the lines written
since the position the hub remembers (inode and byte offset), reduced to what the statistics
need: when, which visitor address, which site, which path (without its query string, which can
hold tokens), the status and the duration. Headers, cookies, user agents and bodies are never
read out of the log. A line cut in half by a write in progress is left for the next call; a
rotated or truncated file is read from its start. The very first call only notes where the end
is, so old history is not swept in.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import stat
from datetime import UTC, datetime
from typing import Any

from armadok.worker.operation import Operation, OperationError

MAX_READ_BYTES = 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_ENTRIES = 2000
_FRACTION = re.compile(r"\.\d+")


def _path() -> str:
    configured = os.environ.get("ARMADOK_TRAEFIK_ACCESSLOG", "")
    if not configured or "\0" in configured:
        raise OperationError("worker.accesslog_not_configured")
    return configured


def _text(value: Any, limit: int) -> str:
    """Return a short printable string (anything else becomes empty)."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value[:limit] if c.isprintable())


def _count(value: Any, ceiling: int) -> int:
    """Return a whole number between 0 and `ceiling` (anything else becomes 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0  # JSON allows NaN and Infinity; a number cannot be made of them
    return max(0, min(int(value), ceiling))


def _seconds(value: Any) -> int | None:
    """Return the Unix time of a Traefik timestamp such as `2026-05-01T12:00:00.123456789Z`."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(
            _FRACTION.sub("", value[:40], count=1).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return int((stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp())


def entry_of(line: bytes) -> dict[str, Any] | None:
    """Reduce one JSON log line to an entry, or None if it is not a usable request line."""
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        raw = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    when = _seconds(raw.get("StartUTC") or raw.get("time"))
    try:
        ip = ipaddress.ip_address(_text(raw.get("ClientHost"), 45))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # one form for one visitor
    address = str(ip)
    status = _count(raw.get("DownstreamStatus"), 999)
    if when is None or status < 100:
        return None
    return {
        "ts": when,
        "ip": address,
        "host": _text(raw.get("RequestHost"), 253),
        # The query string is dropped: it is where tokens and personal data end up.
        "path": _text(str(raw.get("RequestPath") or "/").split("?", 1)[0], 300),
        "method": _text(raw.get("RequestMethod"), 10),
        "status": status,
        "ms": _count(_count(raw.get("Duration"), 10**15) / 1_000_000, 3_600_000),
        "bytes": _count(raw.get("DownstreamContentSize"), 10**12),
        "router": _text(raw.get("RouterName"), 200),
        "service": _text(raw.get("ServiceName"), 200),
    }


def read(args: dict[str, Any]) -> dict[str, Any]:
    """Return the entries written since `{inode, offset}` and the position to continue from.

    Errors: `worker.accesslog_not_configured`, `worker.accesslog_unreadable`.
    """
    inode, offset = args.get("inode"), args.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise OperationError("worker.op_failed")
    try:
        fd = os.open(_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise OperationError("worker.accesslog_unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OperationError("worker.accesslog_unreadable")
        if inode is None:
            return {"configured": True, "inode": info.st_ino, "offset": info.st_size, "entries": []}
        if inode != info.st_ino or offset > info.st_size:
            offset = 0  # rotated or emptied: start again from the top
        os.lseek(fd, offset, os.SEEK_SET)
        data = os.read(fd, MAX_READ_BYTES)
    except OSError:
        raise OperationError("worker.accesslog_unreadable") from None
    finally:
        os.close(fd)
    end = data.rfind(b"\n") + 1
    if end == 0 and len(data) >= MAX_READ_BYTES:
        end = len(data)  # one enormous line: skip it rather than stall on it for ever
    entries: list[dict[str, Any]] = []
    position = 0
    for line in data[:end].splitlines(keepends=True):
        if len(entries) >= MAX_ENTRIES:
            break  # the rest is read next time
        position += len(line)
        if entry := entry_of(line.rstrip(b"\r\n")):
            entries.append(entry)
    return {
        "configured": True,
        "inode": info.st_ino,
        "offset": offset + position,
        "entries": entries,
    }


OPERATIONS: dict[str, Operation] = {"traffic.accesslog": read}
