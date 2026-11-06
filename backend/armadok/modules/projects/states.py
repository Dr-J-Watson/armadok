"""Which project folders Armadok manages on a node.

A folder with a Compose file is *discovered* until someone imports it: it is then *managed* and
can be read, edited, deployed. A folder can also be *ignored* (kept out of sight). Nothing here
touches the node's disk or its containers: importing and un-importing only change what Armadok
shows and allows, so removing a project from Armadok never removes the project.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from armadok.core.db import Migration
from armadok.core.errors import ApiError

MIGRATION = Migration(
    "projects-0002-states",
    """
    CREATE TABLE project_states (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        project TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('managed', 'ignored')),
        labels TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (node_id, project)
    );
    """,
)

MAX_LABELS = 10
MAX_LABEL_LENGTH = 30


def clean_labels(raw: list[str]) -> str:
    """Return the labels as one comma-separated string: trimmed, unique, bounded, no commas."""
    seen: list[str] = []
    for label in raw:
        label = label.strip().replace(",", " ")[:MAX_LABEL_LENGTH].strip()
        if label and label not in seen:
            seen.append(label)
    return ",".join(seen[:MAX_LABELS])


def all_states(conn: sqlite3.Connection, node_id: str) -> dict[str, tuple[str, list[str]]]:
    """Return `{project: (state, labels)}` for every folder that has a record."""
    rows = conn.execute(
        "SELECT project, state, labels FROM project_states WHERE node_id = ?", (node_id,)
    ).fetchall()
    return {r["project"]: (r["state"], [x for x in r["labels"].split(",") if x]) for r in rows}


def managed_names(conn: sqlite3.Connection, node_id: str) -> set[str]:
    """Return the names of the managed projects of a node."""
    return {name for name, (state, _) in all_states(conn, node_id).items() if state == "managed"}


def set_state(
    conn: sqlite3.Connection, node_id: str, projects: list[str], state: str, labels: str = ""
) -> None:
    """Record `state` for each project (a later import replaces an earlier ignore)."""
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        "INSERT INTO project_states (node_id, project, state, labels, created_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (node_id, project) DO UPDATE SET state = excluded.state,"
        " labels = excluded.labels",
        [(node_id, p, state, labels, now) for p in projects],
    )
    conn.commit()


def forget(conn: sqlite3.Connection, node_id: str, project: str) -> str | None:
    """Drop the record of a project; return the state it had (None when there was none)."""
    row = conn.execute(
        "SELECT state FROM project_states WHERE node_id = ? AND project = ?", (node_id, project)
    ).fetchone()
    conn.execute("DELETE FROM project_states WHERE node_id = ? AND project = ?", (node_id, project))
    conn.commit()
    return row["state"] if row else None


def require_managed(conn: sqlite3.Connection, node_id: str, project: str) -> None:
    """Refuse an action on a project that was not imported, as if it did not exist.

    A discovered or ignored folder is only listed to those who may import: reading it, editing
    it or running it is for managed projects, and for everyone else it is simply not there.
    """
    row = conn.execute(
        "SELECT state FROM project_states WHERE node_id = ? AND project = ?", (node_id, project)
    ).fetchone()
    if not row or row["state"] != "managed":
        raise ApiError("projects.not_found", 404)


def claim(conn: sqlite3.Connection, node_id: str, project: str) -> bool:
    """Record a project about to be made here as managed, before its folder exists.

    Written first so that a failure to record it cannot leave a folder nobody may see. Returns
    whether the record is new: the caller gives it back (`forget`) if the folder is not made.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO project_states (node_id, project, state, labels, created_at)"
        " VALUES (?, ?, 'managed', '', ?)",
        (node_id, project, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return cursor.rowcount == 1


def prune(conn: sqlite3.Connection, node_id: str, present: set[str]) -> None:
    """Drop the records of folders that are no longer on the node.

    A different folder that later takes an old name must not inherit its state. Only called with
    a complete listing.
    """
    for name in set(all_states(conn, node_id)) - present:
        conn.execute(
            "DELETE FROM project_states WHERE node_id = ? AND project = ?", (node_id, name)
        )
    conn.commit()
