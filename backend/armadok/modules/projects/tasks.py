"""Deployment tasks: what was started, by whom, how it ended, and what it printed.

The worker runs the action and keeps its output for a while; the hub remembers every task (state,
exit code, output tail), so the history survives a worker restart. Output can contain anything
a build prints, so it is stored encrypted when an encryption key is configured.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core import crypto
from armadok.core.config import Settings
from armadok.core.db import Migration

MIGRATION = Migration(
    "projects-0002-deploy-tasks",
    """
    CREATE TABLE deploy_tasks (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        worker_task TEXT NOT NULL,
        project TEXT NOT NULL,
        action TEXT NOT NULL,
        started_by TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running',
        exit_code INTEGER,
        output TEXT NOT NULL DEFAULT '',
        encrypted INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0,
        notified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX deploy_tasks_node ON deploy_tasks (node_id, created_at);
    """,
)

MAX_LISTED = 50


KEEP_PER_NODE = 200


def create(
    conn: sqlite3.Connection, node_id: str, worker_task: str, project: str, action: str, who: str
) -> str:
    """Record a started task and return its hub id."""
    task_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO deploy_tasks (id, node_id, worker_task, project, action, started_by,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, node_id, worker_task, project, action, who, datetime.now(UTC).isoformat()),
    )
    # Keep the history bounded: only the newest tasks of a node are kept.
    conn.execute(
        "DELETE FROM deploy_tasks WHERE node_id = ? AND id NOT IN ("
        " SELECT id FROM deploy_tasks WHERE node_id = ? ORDER BY created_at DESC LIMIT ?)",
        (node_id, node_id, KEEP_PER_NODE),
    )
    conn.commit()
    return task_id


def get(conn: sqlite3.Connection, node_id: str, task_id: str) -> sqlite3.Row | None:
    """Return a task of this node (a task id never opens another node's task)."""
    return conn.execute(
        "SELECT * FROM deploy_tasks WHERE id = ? AND node_id = ?", (task_id, node_id)
    ).fetchone()


def finish(
    conn: sqlite3.Connection,
    settings: Settings,
    task_id: str,
    state: str,
    exit_code: int | None,
    output: str,
    truncated: bool,
) -> bool:
    """Store a task's end; return True only for the call that moved it out of "running"."""
    sealed, encrypted = (
        (crypto.encrypt(settings, output), 1) if settings.encryption_key else (output, 0)
    )
    cur = conn.execute(
        "UPDATE deploy_tasks SET state = ?, exit_code = ?, output = ?, encrypted = ?,"
        " truncated = ?, finished_at = ? WHERE id = ? AND state = 'running'",
        (
            state,
            exit_code,
            sealed,
            encrypted,
            int(truncated),
            datetime.now(UTC).isoformat(),
            task_id,
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def claim_notification(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True once per task: the caller that gets True sends the notification."""
    cur = conn.execute(
        "UPDATE deploy_tasks SET notified = 1 WHERE id = ? AND notified = 0", (task_id,)
    )
    conn.commit()
    return cur.rowcount == 1


def view(settings: Settings, row: sqlite3.Row) -> dict[str, Any]:
    """Return a task as the API shows it (output decrypted)."""
    output = crypto.decrypt(settings, row["output"]) if row["encrypted"] else row["output"]
    return {
        "id": row["id"],
        "project": row["project"],
        "action": row["action"],
        "state": row["state"],
        "exit_code": row["exit_code"],
        "output": output,
        "truncated": bool(row["truncated"]),
        "started_by": row["started_by"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def recent(conn: sqlite3.Connection, node_id: str, project: str | None) -> list[sqlite3.Row]:
    """Return the latest tasks of a node (optionally of one project), newest first."""
    where, args = ["node_id = ?"], [node_id]
    if project:
        where.append("project = ?")
        args.append(project)
    return conn.execute(
        f"SELECT * FROM deploy_tasks WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
        (*args, MAX_LISTED),
    ).fetchall()
