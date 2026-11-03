"""Storage of availability probes and their results."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "uptime-0001-probes",
    """
    CREATE TABLE probes (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        scheme TEXT,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        path TEXT NOT NULL DEFAULT '/',
        verify_tls INTEGER NOT NULL DEFAULT 1,
        expect_status INTEGER,
        interval_seconds INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'unknown',
        failures INTEGER NOT NULL DEFAULT 0,
        last_checked TEXT,
        last_change TEXT,
        last_reason TEXT
    );
    CREATE INDEX probes_node ON probes (node_id);
    CREATE TABLE probe_results (
        probe_id TEXT NOT NULL REFERENCES probes(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        ok INTEGER NOT NULL,
        latency_ms INTEGER,
        reason TEXT
    );
    CREATE INDEX probe_results_probe ON probe_results (probe_id, at);
    """,
)

RECOVERY = Migration(
    "uptime-0002-recovery",
    "ALTER TABLE probes ADD COLUMN successes INTEGER NOT NULL DEFAULT 0;",
)

# Results are kept for a week and at most this many per probe: the history is a glance at the
# recent past, not an archive, and the table must not grow without bound.
RETENTION = timedelta(days=7)
MAX_RESULTS = 2000
FIELDS = (
    "id node_id name kind scheme host port path verify_tls expect_status interval_seconds"
    " enabled created_by created_at state last_checked last_change last_reason"
).split()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def view(row: sqlite3.Row) -> dict[str, Any]:
    """Return the public face of a probe."""
    data = {k: row[k] for k in FIELDS}
    data["verify_tls"] = bool(row["verify_tls"])
    data["enabled"] = bool(row["enabled"])
    return data


def list_for_node(conn: sqlite3.Connection, node_id: str) -> list[sqlite3.Row]:
    """Return a node's probes, oldest first."""
    return conn.execute(
        "SELECT * FROM probes WHERE node_id = ? ORDER BY created_at, id", (node_id,)
    ).fetchall()


def get(conn: sqlite3.Connection, probe_id: str) -> sqlite3.Row | None:
    """Return one probe, or None."""
    return conn.execute("SELECT * FROM probes WHERE id = ?", (probe_id,)).fetchone()


def count_for_node(conn: sqlite3.Connection, node_id: str) -> int:
    """Return how many probes a node has."""
    return conn.execute("SELECT COUNT(*) FROM probes WHERE node_id = ?", (node_id,)).fetchone()[0]


def create(conn: sqlite3.Connection, node_id: str, who: str, fields: dict[str, Any]) -> str:
    """Store a probe and return its id."""
    probe_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO probes (id, node_id, name, kind, scheme, host, port, path, verify_tls,"
        " expect_status, interval_seconds, created_by, created_at)"
        " VALUES (:id, :node_id, :name, :kind, :scheme, :host, :port, :path, :verify_tls,"
        " :expect_status, :interval_seconds, :created_by, :created_at)",
        {
            **fields,
            "id": probe_id,
            "node_id": node_id,
            "created_by": who,
            "created_at": _now(),
        },
    )
    conn.commit()
    return probe_id


def update(conn: sqlite3.Connection, probe_id: str, fields: dict[str, Any]) -> None:
    """Change the editable fields of a probe (keys are filtered against a fixed list)."""
    allowed = {"name", "path", "verify_tls", "expect_status", "interval_seconds", "enabled"}
    keys = [k for k in fields if k in allowed]
    if not keys:
        return
    conn.execute(
        f"UPDATE probes SET {', '.join(f'{k} = :{k}' for k in keys)} WHERE id = :id",  # noqa: S608
        {**{k: fields[k] for k in keys}, "id": probe_id},
    )
    conn.commit()


def delete(conn: sqlite3.Connection, probe_id: str) -> None:
    """Remove a probe and its history."""
    conn.execute("DELETE FROM probes WHERE id = ?", (probe_id,))
    conn.commit()


def count_all(conn: sqlite3.Connection) -> int:
    """Return how many probes exist on the whole platform."""
    return conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0]


def due(conn: sqlite3.Connection, now: datetime, limit: int) -> list[sqlite3.Row]:
    """Return enabled probes whose next check is due, longest waiting first."""
    return conn.execute(
        "SELECT * FROM probes WHERE enabled = 1 AND (last_checked IS NULL OR"
        " julianday(last_checked) + interval_seconds / 86400.0 <= julianday(?))"
        " ORDER BY COALESCE(last_checked, '') LIMIT ?",
        (now.isoformat(), limit),
    ).fetchall()


def mark_checked(conn: sqlite3.Connection, probe_id: str) -> None:
    """Record that a check started, so an unreachable worker is not asked again at once."""
    conn.execute("UPDATE probes SET last_checked = ? WHERE id = ?", (_now(), probe_id))
    conn.commit()


def record_result(
    conn: sqlite3.Connection, probe_id: str, ok: bool, latency_ms: int | None, reason: str | None
) -> None:
    """Append a result and trim the history."""
    conn.execute(
        "INSERT INTO probe_results (probe_id, at, ok, latency_ms, reason) VALUES (?, ?, ?, ?, ?)",
        (probe_id, _now(), int(ok), latency_ms, reason),
    )
    cutoff = (datetime.now(UTC) - RETENTION).isoformat()
    conn.execute("DELETE FROM probe_results WHERE probe_id = ? AND at < ?", (probe_id, cutoff))
    conn.execute(
        "DELETE FROM probe_results WHERE probe_id = ? AND rowid NOT IN"
        " (SELECT rowid FROM probe_results WHERE probe_id = ? ORDER BY at DESC LIMIT ?)",
        (probe_id, probe_id, MAX_RESULTS),
    )
    conn.commit()


def reset_state(conn: sqlite3.Connection, probe_id: str) -> None:
    """Forget the state (a paused probe says nothing about the site any more)."""
    conn.execute(
        "UPDATE probes SET state = 'unknown', failures = 0, successes = 0, last_reason = NULL"
        " WHERE id = ?",
        (probe_id,),
    )
    conn.commit()


def set_state(
    conn: sqlite3.Connection,
    probe_id: str,
    state: str,
    failures: int,
    successes: int,
    reason: str | None,
) -> None:
    """Store the probe's state; `last_change` moves only when the state itself changes."""
    conn.execute(
        "UPDATE probes SET last_change = CASE WHEN state != ? THEN ? ELSE last_change END,"
        " state = ?, failures = ?, successes = ?, last_reason = ? WHERE id = ?",
        (state, _now(), state, failures, successes, reason, probe_id),
    )
    conn.commit()


def history(conn: sqlite3.Connection, probe_id: str, limit: int) -> list[dict[str, Any]]:
    """Return the latest results, newest first."""
    rows = conn.execute(
        "SELECT at, ok, latency_ms, reason FROM probe_results WHERE probe_id = ?"
        " ORDER BY at DESC LIMIT ?",
        (probe_id, limit),
    ).fetchall()
    return [
        {"at": r["at"], "ok": bool(r["ok"]), "latency_ms": r["latency_ms"], "reason": r["reason"]}
        for r in rows
    ]


def availability(conn: sqlite3.Connection, probe_id: str) -> float | None:
    """Return the share of successful checks over the kept history, or None without results."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(ok), 0) AS good FROM probe_results WHERE probe_id = ?",
        (probe_id,),
    ).fetchone()
    return round(100 * row["good"] / row["n"], 2) if row["n"] else None
