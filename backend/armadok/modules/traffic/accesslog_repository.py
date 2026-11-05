"""Storage of access-log entries (short retention) and of the read position per machine."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.db import Migration

# Raw entries carry visitor addresses (personal data): they are kept a week, no longer.
RETENTION = timedelta(days=7)
MAX_EVENTS_PER_NODE = 500_000

MIGRATION = Migration(
    "traffic-0002-accesslog",
    """
    CREATE TABLE access_cursor (
        node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
        inode INTEGER NOT NULL,
        byte_offset INTEGER NOT NULL
    );
    CREATE TABLE access_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        ts INTEGER NOT NULL,
        ip TEXT NOT NULL,
        host TEXT NOT NULL,
        path TEXT NOT NULL,
        method TEXT NOT NULL,
        status INTEGER NOT NULL,
        ms INTEGER NOT NULL,
        bytes INTEGER NOT NULL,
        router TEXT NOT NULL,
        service TEXT NOT NULL
    );
    CREATE INDEX access_events_node_ts ON access_events (node_id, ts);
    """,
)

_COLUMNS = ("ts", "ip", "host", "path", "method", "status", "ms", "bytes", "router", "service")


def get_cursor(conn: sqlite3.Connection, node_id: str) -> tuple[int, int] | None:
    """Return `(inode, offset)` where reading stopped, or None if the log was never read."""
    row = conn.execute(
        "SELECT inode, byte_offset FROM access_cursor WHERE node_id = ?", (node_id,)
    ).fetchone()
    return (row["inode"], row["byte_offset"]) if row else None


def store(
    conn: sqlite3.Connection,
    node_id: str,
    expected: tuple[int, int] | None,
    cursor: tuple[int, int],
    entries: list[dict[str, Any]],
) -> bool:
    """Keep new entries and move the read position, in one transaction (the caller commits).

    `expected` is the position the entries were read from. If it is no longer the stored one
    (the entries were purged while the worker was reading), nothing is kept and False comes
    back: a purge is never partly undone. The per-machine cap is enforced here.
    """
    if get_cursor(conn, node_id) != expected:
        return False
    conn.executemany(
        "INSERT INTO access_events (node_id, "
        + ", ".join(_COLUMNS)
        + ") VALUES (?, "
        + ", ".join("?" for _ in _COLUMNS)
        + ")",
        [(node_id, *(e[c] for c in _COLUMNS)) for e in entries],
    )
    conn.execute(
        "INSERT INTO access_cursor (node_id, inode, byte_offset) VALUES (?, ?, ?)"
        " ON CONFLICT(node_id) DO UPDATE SET inode = excluded.inode,"
        " byte_offset = excluded.byte_offset",
        (node_id, *cursor),
    )
    _enforce_cap(conn, node_id)
    return True


def _enforce_cap(conn: sqlite3.Connection, node_id: str) -> None:
    """Drop the oldest entries of a machine beyond the cap."""
    conn.execute(
        "DELETE FROM access_events WHERE node_id = ? AND id <= ("
        " SELECT id FROM access_events WHERE node_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (node_id, node_id, MAX_EVENTS_PER_NODE),
    )


def purge(conn: sqlite3.Connection) -> None:
    """Drop entries past the retention, and the oldest ones beyond the cap (caller commits)."""
    conn.execute(
        "DELETE FROM access_events WHERE ts < ?",
        (int((datetime.now(UTC) - RETENTION).timestamp()),),
    )
    for row in conn.execute("SELECT DISTINCT node_id FROM access_events").fetchall():
        _enforce_cap(conn, row["node_id"])


def forget_node(conn: sqlite3.Connection, node_id: str) -> int:
    """Delete every kept entry of a machine and its position; return how many (caller commits)."""
    done = conn.execute("DELETE FROM access_events WHERE node_id = ?", (node_id,)).rowcount
    conn.execute("DELETE FROM access_cursor WHERE node_id = ?", (node_id,))
    return done


def per_ip(
    conn: sqlite3.Connection, node_id: str, since: int, service: str | None
) -> list[sqlite3.Row]:
    """Return one row per address: requests, errors, bytes, first and last seen.

    Not cut short: the caller merges addresses into networks, and a cut would undercount them.
    """
    return conn.execute(
        "SELECT ip, COUNT(*) AS requests, SUM(status >= 400) AS errors, SUM(bytes) AS bytes,"
        " MIN(ts) AS first_seen, MAX(ts) AS last_seen FROM access_events"
        " WHERE node_id = ? AND ts >= ? AND (? IS NULL OR service = ?)"
        " GROUP BY ip",
        (node_id, since, service, service),
    ).fetchall()


def per_path(
    conn: sqlite3.Connection, node_id: str, since: int, service: str | None, limit: int
) -> list[sqlite3.Row]:
    """Return the most requested paths with their errors."""
    return conn.execute(
        "SELECT path, COUNT(*) AS requests, SUM(status >= 400) AS errors FROM access_events"
        " WHERE node_id = ? AND ts >= ? AND (? IS NULL OR service = ?)"
        " GROUP BY path ORDER BY requests DESC LIMIT ?",
        (node_id, since, service, service, limit),
    ).fetchall()


def per_class(
    conn: sqlite3.Connection, node_id: str, since: int, service: str | None
) -> dict[str, int]:
    """Return the number of requests by status class (`2xx`...)."""
    rows = conn.execute(
        "SELECT status / 100 AS klass, COUNT(*) AS n FROM access_events"
        " WHERE node_id = ? AND ts >= ? AND (? IS NULL OR service = ?) GROUP BY klass",
        (node_id, since, service, service),
    )
    return {f"{r['klass']}xx": r["n"] for r in rows}


def recent(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    since: int,
    service: str | None,
    klass: int | None,
    path: str | None,
    ip: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    """Return the newest entries that match the filters (`path` is a substring)."""
    like = (
        None
        if path is None
        else "%" + path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )
    return conn.execute(
        "SELECT ts, ip, host, path, method, status, ms, service FROM access_events"
        " WHERE node_id = ? AND ts >= ? AND (? IS NULL OR service = ?)"
        " AND (? IS NULL OR status / 100 = ?) AND (? IS NULL OR path LIKE ? ESCAPE '\\')"
        " AND (? IS NULL OR ip = ?) ORDER BY ts DESC, id DESC LIMIT ?",
        (node_id, since, service, service, klass, klass, like, like, ip, ip, limit),
    ).fetchall()


def status(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    """Return how many entries are kept, the newest one's time, and whether reading has begun."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(ts) AS newest FROM access_events WHERE node_id = ? AND ts >= ?",
        (node_id, int((datetime.now(UTC) - RETENTION).timestamp())),
    ).fetchone()
    return {
        "kept": row["n"],
        "newest": row["newest"],
        "reading": get_cursor(conn, node_id) is not None,
    }
