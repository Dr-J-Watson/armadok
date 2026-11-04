"""Storage of traffic figures: the last counters seen, and per-five-minutes totals."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.db import Migration

STEP = 300  # seconds per stored bucket
RETENTION = timedelta(days=30)

MIGRATION = Migration(
    "traffic-0001-metrics",
    """
    CREATE TABLE traffic_state (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        service TEXT NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY (node_id, service)
    );
    CREATE TABLE traffic_buckets (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        service TEXT NOT NULL,
        ts INTEGER NOT NULL,
        r2 REAL NOT NULL DEFAULT 0,
        r3 REAL NOT NULL DEFAULT 0,
        r4 REAL NOT NULL DEFAULT 0,
        r5 REAL NOT NULL DEFAULT 0,
        ro REAL NOT NULL DEFAULT 0,
        bytes_in REAL NOT NULL DEFAULT 0,
        bytes_out REAL NOT NULL DEFAULT 0,
        dur_sum REAL NOT NULL DEFAULT 0,
        dur_count REAL NOT NULL DEFAULT 0,
        hist TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (node_id, service, ts)
    );
    CREATE TABLE traffic_node (
        node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
        last_try TEXT,
        last_ok TEXT,
        last_error TEXT,
        reload_ok INTEGER,
        reload_failures REAL,
        servers TEXT NOT NULL DEFAULT '{}'
    );
    """,
)

CLASS_COLUMN = {"2xx": "r2", "3xx": "r3", "4xx": "r4", "5xx": "r5", "other": "ro"}


def _now() -> datetime:
    return datetime.now(UTC)


def get_state(conn: sqlite3.Connection, node_id: str) -> dict[str, dict[str, Any]]:
    """Return the last counters seen for each service of a node."""
    rows = conn.execute("SELECT service, data FROM traffic_state WHERE node_id = ?", (node_id,))
    return {r["service"]: json.loads(r["data"]) for r in rows}


def set_state(conn: sqlite3.Connection, node_id: str, services: dict[str, dict[str, Any]]) -> None:
    """Replace the remembered counters of a node (the caller commits)."""
    conn.execute("DELETE FROM traffic_state WHERE node_id = ?", (node_id,))
    conn.executemany(
        "INSERT INTO traffic_state (node_id, service, data) VALUES (?, ?, ?)",
        [(node_id, name, json.dumps(data)) for name, data in services.items()],
    )


def service_names(conn: sqlite3.Connection, node_id: str) -> set[str]:
    """Return the service names with figures kept for a node."""
    rows = conn.execute(
        "SELECT DISTINCT service FROM traffic_buckets WHERE node_id = ?", (node_id,)
    )
    return {r["service"] for r in rows}


def add_bucket(
    conn: sqlite3.Connection,
    node_id: str,
    service: str,
    ts: int,
    delta: dict[str, Any],
    max_hist_keys: int = 30,
) -> None:
    """Add a delta to the five-minute bucket that holds `ts` (the caller commits)."""
    row = conn.execute(
        "SELECT hist FROM traffic_buckets WHERE node_id = ? AND service = ? AND ts = ?",
        (node_id, service, ts),
    ).fetchone()
    hist: dict[str, float] = json.loads(row["hist"]) if row else {}
    for le, value in delta["hist"].items():
        if le in hist or len(hist) < max_hist_keys:
            hist[le] = hist.get(le, 0.0) + value
    if row is None:
        conn.execute(
            "INSERT INTO traffic_buckets (node_id, service, ts) VALUES (?, ?, ?)",
            (node_id, service, ts),
        )
    conn.execute(
        "UPDATE traffic_buckets SET r2 = r2 + ?, r3 = r3 + ?, r4 = r4 + ?, r5 = r5 + ?,"
        " ro = ro + ?, bytes_in = bytes_in + ?, bytes_out = bytes_out + ?,"
        " dur_sum = dur_sum + ?, dur_count = dur_count + ?, hist = ?"
        " WHERE node_id = ? AND service = ? AND ts = ?",
        (
            delta["r2"],
            delta["r3"],
            delta["r4"],
            delta["r5"],
            delta["ro"],
            delta["bytes_in"],
            delta["bytes_out"],
            delta["dur_sum"],
            delta["dur_count"],
            json.dumps(hist),
            node_id,
            service,
            ts,
        ),
    )


def buckets(conn: sqlite3.Connection, node_id: str, since: int, until: int) -> list[sqlite3.Row]:
    """Return the buckets between two instants, oldest first."""
    return conn.execute(
        "SELECT * FROM traffic_buckets WHERE node_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
        (node_id, since, until),
    ).fetchall()


def purge_old(conn: sqlite3.Connection) -> None:
    """Drop what is older than the retention (the caller commits)."""
    conn.execute(
        "DELETE FROM traffic_buckets WHERE ts < ?", (int((_now() - RETENTION).timestamp()),)
    )


def note_try(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    error: str | None,
    reload_ok: bool | None = None,
    reload_failures: float | None = None,
    servers: dict[str, dict[str, int]] | None = None,
) -> None:
    """Remember how the last attempt to read a node's metrics went."""
    now = _now().isoformat()
    conn.execute("INSERT OR IGNORE INTO traffic_node (node_id) VALUES (?)", (node_id,))
    if error is None:
        conn.execute(
            "UPDATE traffic_node SET last_try = ?, last_ok = ?, last_error = NULL, reload_ok = ?,"
            " reload_failures = ?, servers = ? WHERE node_id = ?",
            (
                now,
                now,
                None if reload_ok is None else int(reload_ok),
                reload_failures,
                json.dumps(servers or {}),
                node_id,
            ),
        )
    else:
        conn.execute(
            "UPDATE traffic_node SET last_try = ?, last_error = ? WHERE node_id = ?",
            (now, error, node_id),
        )


def node_status(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    """Return how reading a node's metrics is going, or None if it was never tried."""
    row = conn.execute("SELECT * FROM traffic_node WHERE node_id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    return {
        "last_try": row["last_try"],
        "last_ok": row["last_ok"],
        "last_error": row["last_error"],
        "reload_ok": None if row["reload_ok"] is None else bool(row["reload_ok"]),
        "reload_failures": row["reload_failures"],
        "servers": json.loads(row["servers"]),
    }


def nodes_to_read(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return the machines that could have Traefik metrics, with how their last try went."""
    return conn.execute(
        "SELECT n.id, t.last_try, t.last_error FROM nodes n"
        " LEFT JOIN traffic_node t ON t.node_id = n.id"
        " WHERE n.deleted_at IS NULL AND n.suspended_at IS NULL AND n.type IN ('network', 'full')"
    ).fetchall()
