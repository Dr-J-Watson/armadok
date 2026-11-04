"""Storage of vulnerability findings and of the last scan."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "vulns-0001-findings",
    """
    CREATE TABLE vuln_findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component TEXT NOT NULL,
        version TEXT NOT NULL,
        vuln_id TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        fixed TEXT NOT NULL,
        aliases TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        ignored_reason TEXT,
        ignored_by TEXT,
        ignored_at TEXT,
        UNIQUE (component, version, vuln_id)
    );
    CREATE TABLE vuln_scan (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        finished_at TEXT,
        ok INTEGER,
        error TEXT,
        components INTEGER
    );
    """,
)


MISSED_MIGRATION = Migration(
    "vulns-0002-missed-notified",
    """
    ALTER TABLE vuln_findings ADD COLUMN missed INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE vuln_findings ADD COLUMN notified INTEGER NOT NULL DEFAULT 0;
    """,
)
MISSES_BEFORE_DELETE = 2  # a finding must be absent from two scans in a row to be forgotten


def _now() -> str:
    return datetime.now(UTC).isoformat()


def store(
    conn: sqlite3.Connection,
    findings: list[dict[str, Any]],
    installed: set[tuple[str, str]],
    truncated: set[tuple[str, str]],
) -> None:
    """Merge the latest scan into what is known.

    A finding that is seen keeps its date, its "accepted" state and its notified flag. One that
    belongs to a package no longer installed (or at another version) is dropped at once. One
    that is simply absent (the database withdrew it, or the list was cut short) is dropped only
    after it was absent from two scans in a row, so a hiccup cannot erase an exception.
    """
    seen = {(f["component"], f["version"], f["id"]) for f in findings}
    for row in conn.execute(
        "SELECT id, component, version, vuln_id, missed FROM vuln_findings"
    ).fetchall():
        key = (row["component"], row["version"], row["vuln_id"])
        if key in seen:
            continue
        if (row["component"], row["version"]) not in installed:
            conn.execute("DELETE FROM vuln_findings WHERE id = ?", (row["id"],))
        elif (row["component"], row["version"]) in truncated:
            continue  # the list for this package was cut short: absence proves nothing
        elif row["missed"] + 1 >= MISSES_BEFORE_DELETE:
            conn.execute("DELETE FROM vuln_findings WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE vuln_findings SET missed = missed + 1 WHERE id = ?", (row["id"],))
    for f in findings:
        updated = conn.execute(
            "UPDATE vuln_findings SET severity = ?, summary = ?, fixed = ?, aliases = ?, missed = 0"
            " WHERE component = ? AND version = ? AND vuln_id = ?",
            (
                f["severity"],
                f["summary"],
                f["fixed"],
                json.dumps(f["aliases"]),
                f["component"],
                f["version"],
                f["id"],
            ),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO vuln_findings (component, version, vuln_id, severity, summary,"
                " fixed, aliases, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f["component"],
                    f["version"],
                    f["id"],
                    f["severity"],
                    f["summary"],
                    f["fixed"],
                    json.dumps(f["aliases"]),
                    _now(),
                ),
            )
    set_scan(conn, True, None, len(installed))


def unnotified_serious(conn: sqlite3.Connection, severities: tuple[str, ...]) -> list[sqlite3.Row]:
    """Return serious findings nobody was told about yet and that are not accepted."""
    marks = ",".join("?" * len(severities))
    return conn.execute(
        f"SELECT * FROM vuln_findings WHERE notified = 0 AND ignored_reason IS NULL"  # noqa: S608
        f" AND severity IN ({marks})",
        severities,
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Remember that these findings were announced."""
    conn.executemany("UPDATE vuln_findings SET notified = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def set_scan(conn: sqlite3.Connection, ok: bool, error: str | None, components: int = 0) -> None:
    """Remember how the last scan went."""
    conn.execute(
        "INSERT INTO vuln_scan (id, finished_at, ok, error, components) VALUES (1, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET finished_at = excluded.finished_at, ok = excluded.ok,"
        " error = excluded.error, components = excluded.components",
        (_now(), int(ok), error, components),
    )
    conn.commit()


def last_scan(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the last scan's outcome, or None if there never was one."""
    row = conn.execute("SELECT * FROM vuln_scan WHERE id = 1").fetchone()
    if row is None:
        return None
    return {
        "finished_at": row["finished_at"],
        "ok": bool(row["ok"]),
        "error": row["error"],
        "components": row["components"],
    }


def findings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every finding, most serious first."""
    rank = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "unknown": 4}
    rows = conn.execute("SELECT * FROM vuln_findings").fetchall()
    out = [
        {
            "id": r["id"],
            "component": r["component"],
            "version": r["version"],
            "vuln_id": r["vuln_id"],
            "severity": r["severity"],
            "summary": r["summary"],
            "fixed": r["fixed"],
            "aliases": json.loads(r["aliases"]),
            "first_seen": r["first_seen"],
            "ignored_reason": r["ignored_reason"],
        }
        for r in rows
    ]
    return sorted(
        out, key=lambda f: (f["ignored_reason"] is not None, rank[f["severity"]], f["component"])
    )


def ignore(conn: sqlite3.Connection, finding_id: int, reason: str | None, who: str) -> bool:
    """Set (or clear, with `reason=None`) the justified exception on a finding.

    Setting one on a finding that already has one is refused (returns False): nobody overwrites
    somebody else's reason silently.
    """
    cur = conn.execute(
        "UPDATE vuln_findings SET ignored_reason = ?, ignored_by = ?, ignored_at = ?"
        " WHERE id = ? AND (? IS NULL OR ignored_reason IS NULL)",
        (reason, who if reason else None, _now() if reason else None, finding_id, reason),
    )
    conn.commit()
    return cur.rowcount == 1


def get(conn: sqlite3.Connection, finding_id: int) -> sqlite3.Row | None:
    """Return one finding, or None."""
    return conn.execute("SELECT * FROM vuln_findings WHERE id = ?", (finding_id,)).fetchone()
