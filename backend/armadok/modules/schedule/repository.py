"""Storage of scheduled tasks."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration
from armadok.modules.schedule import timing

MIGRATION = Migration(
    "schedule-0001-tasks",
    """
    CREATE TABLE scheduled_tasks (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        schedule TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        next_run TEXT NOT NULL,
        last_run TEXT,
        last_result TEXT
    );
    CREATE INDEX scheduled_tasks_node ON scheduled_tasks (node_id);
    CREATE INDEX scheduled_tasks_due ON scheduled_tasks (enabled, next_run);
    """,
)


MISSED = Migration(
    "schedule-0002-missed-slot",
    "ALTER TABLE scheduled_tasks ADD COLUMN missed_slot TEXT;",
)


def _now() -> datetime:
    return datetime.now(UTC)


def view(row: sqlite3.Row) -> dict[str, Any]:
    """Return the public face of a task."""
    return {
        "id": row["id"],
        "node_id": row["node_id"],
        "name": row["name"],
        "action": row["action"],
        "target": json.loads(row["target"]),
        "schedule": json.loads(row["schedule"]),
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "next_run": row["next_run"] if row["enabled"] else None,
        "last_run": row["last_run"],
        "last_result": row["last_result"],
        # The time of a run missed while the hub was stopped, until its manager decides.
        "missed_slot": row["missed_slot"],
    }


def list_for_node(conn: sqlite3.Connection, node_id: str) -> list[sqlite3.Row]:
    """Return a node's tasks, oldest first."""
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE node_id = ? ORDER BY created_at, id", (node_id,)
    ).fetchall()


def get(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    """Return one task, or None."""
    return conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()


def count(conn: sqlite3.Connection, node_id: str | None = None) -> int:
    """Return how many tasks exist (on one node, or everywhere)."""
    if node_id is None:
        return conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks WHERE node_id = ?", (node_id,)
    ).fetchone()[0]


def create(
    conn: sqlite3.Connection,
    node_id: str,
    who: str,
    name: str,
    action: str,
    target: dict[str, Any],
    schedule: dict[str, Any],
) -> str:
    """Store a task and return its id."""
    task_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO scheduled_tasks (id, node_id, name, action, target, schedule, created_by,"
        " created_at, next_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            node_id,
            name,
            action,
            json.dumps(target, sort_keys=True),
            json.dumps(schedule, sort_keys=True),
            who,
            _now().isoformat(),
            timing.next_run(schedule, _now()).isoformat(),
        ),
    )
    conn.commit()
    return task_id


def update(
    conn: sqlite3.Connection,
    task_id: str,
    name: str | None,
    enabled: bool | None,
    schedule: dict[str, Any] | None,
) -> None:
    """Change the name, the on/off switch or the schedule (the next run follows)."""
    row = get(conn, task_id)
    if row is None:
        return
    new_schedule = schedule if schedule is not None else json.loads(row["schedule"])
    # The next run moves only when the timing changes or the task is switched on again: renaming
    # a daily task must not push it back, over and over, so that it never runs.
    retimed = schedule is not None or (enabled and not row["enabled"])
    conn.execute(
        "UPDATE scheduled_tasks SET name = ?, enabled = ?, schedule = ?, next_run = ? WHERE id = ?",
        (
            name if name is not None else row["name"],
            int(enabled) if enabled is not None else row["enabled"],
            json.dumps(new_schedule, sort_keys=True),
            timing.next_run(new_schedule, _now()).isoformat() if retimed else row["next_run"],
            task_id,
        ),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task."""
    conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    conn.commit()


def due(conn: sqlite3.Connection, now: datetime, limit: int) -> list[sqlite3.Row]:
    """Return enabled tasks whose time has come, oldest first."""
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND julianday(next_run) <= julianday(?)"
        " ORDER BY next_run LIMIT ?",
        (now.isoformat(), limit),
    ).fetchall()


def advance(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Move the next run past now, before the task runs; True only for the one caller that did.

    The change names the slot it replaces, so even two hubs sharing a database cannot both
    take the same slot: a task never runs twice for one slot (and a crash right after this
    loses the slot rather than doubling it).
    """
    try:
        following = timing.next_run(json.loads(row["schedule"]), _now()).isoformat()
    except ValueError:
        # A rule that no longer matches any date (a 29 February that is years away, say): the
        # task is paused with a reason instead of failing every round for all the others.
        cur = conn.execute(
            "UPDATE scheduled_tasks SET enabled = 0, last_result = 'schedule.no_next_run'"
            " WHERE id = ? AND next_run = ?",
            (row["id"], row["next_run"]),
        )
        conn.commit()
        return cur.rowcount == 1
    cur = conn.execute(
        "UPDATE scheduled_tasks SET next_run = ? WHERE id = ? AND next_run = ?",
        (following, row["id"], row["next_run"]),
    )
    conn.commit()
    return cur.rowcount == 1


def set_skipped(conn: sqlite3.Connection, task_id: str, slot: str) -> None:
    """Remember that the run due at `slot` was missed (the hub was down), awaiting a decision.

    `last_run` stays what it was.
    """
    conn.execute(
        "UPDATE scheduled_tasks SET last_result = 'skipped', missed_slot = ? WHERE id = ?",
        (slot, task_id),
    )
    conn.commit()


def clear_missed(conn: sqlite3.Connection, task_id: str, slot: str) -> bool:
    """Forget a missed run; True only if `slot` was still the one waiting (one decision wins)."""
    cur = conn.execute(
        "UPDATE scheduled_tasks SET missed_slot = NULL WHERE id = ? AND missed_slot = ?",
        (task_id, slot),
    )
    conn.commit()
    return cur.rowcount == 1


def set_result(conn: sqlite3.Connection, task_id: str, result: str) -> None:
    """Remember when the task last ran and how it went (`ok`, `skipped` or an error code)."""
    conn.execute(
        "UPDATE scheduled_tasks SET last_run = ?, last_result = ? WHERE id = ?",
        (_now().isoformat(), result, task_id),
    )
    conn.commit()
