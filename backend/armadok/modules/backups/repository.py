"""Storage of backup and restore jobs: what was started, by whom, how it ended."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "backups-0001-jobs",
    """
    CREATE TABLE backup_jobs (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        volume TEXT NOT NULL,
        archive TEXT,
        size INTEGER,
        worker_task TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running',
        error TEXT,
        started_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX backup_jobs_node ON backup_jobs (node_id, created_at);
    """,
)

ACTOR = Migration("backups-0002-actor", "ALTER TABLE backup_jobs ADD COLUMN actor TEXT;")

KEEP_PER_NODE = 200
STALE_AFTER = timedelta(hours=3)  # the worker's own limit is two hours


def create(
    conn: sqlite3.Connection,
    node_id: str,
    kind: str,
    volume: str,
    archive: str | None,
    worker_task: str,
    who: str,
    actor: str,
) -> str:
    """Record a started job and return its id."""
    job_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO backup_jobs (id, node_id, kind, volume, archive, worker_task, started_by,"
        " created_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            node_id,
            kind,
            volume,
            archive,
            worker_task,
            who,
            datetime.now(UTC).isoformat(),
            actor,
        ),
    )
    conn.execute(
        "DELETE FROM backup_jobs WHERE node_id = ? AND id NOT IN ("
        " SELECT id FROM backup_jobs WHERE node_id = ? ORDER BY created_at DESC LIMIT ?)",
        (node_id, node_id, KEEP_PER_NODE),
    )
    conn.commit()
    return job_id


def running(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every job still running, oldest first."""
    return conn.execute(
        "SELECT * FROM backup_jobs WHERE state = 'running' ORDER BY created_at"
    ).fetchall()


def finish(
    conn: sqlite3.Connection,
    job_id: str,
    state: str,
    error: str | None,
    archive: str | None,
    size: int | None,
) -> bool:
    """Store a job's end; return True only for the call that moved it out of "running"."""
    cur = conn.execute(
        "UPDATE backup_jobs SET state = ?, error = ?, archive = COALESCE(?, archive), size = ?,"
        " finished_at = ? WHERE id = ? AND state = 'running'",
        (state, error, archive, size, datetime.now(UTC).isoformat(), job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def recent(conn: sqlite3.Connection, node_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """Return a node's latest jobs, newest first."""
    rows = conn.execute(
        "SELECT * FROM backup_jobs WHERE node_id = ? ORDER BY created_at DESC LIMIT ?",
        (node_id, limit),
    ).fetchall()
    keys = (
        "id",
        "kind",
        "volume",
        "archive",
        "size",
        "state",
        "error",
        "created_at",
        "finished_at",
    )
    return [{k: r[k] for k in keys} for r in rows]


def is_stale(row: sqlite3.Row) -> bool:
    """Return True for a job that has run longer than any worker would let it."""
    return datetime.now(UTC) - datetime.fromisoformat(row["created_at"]) > STALE_AFTER
