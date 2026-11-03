"""Audit log: who did what, on which object, with what outcome.

Every action writes one entry, whether it comes from the interface or an API
key. Event types must be declared by a module, so the catalogue of events stays
complete. Never put secret values or note contents in `details`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from armadok.core.db import Migration

AUDIT_MIGRATION = Migration(
    "core-0001-audit",
    """
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        event TEXT NOT NULL,
        object_type TEXT,
        object_id TEXT,
        outcome TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX audit_log_at ON audit_log (at);
    CREATE INDEX audit_log_actor ON audit_log (actor);
    """,
)


class UnknownAuditEvent(ValueError):
    """Raised when code writes an event type no module declared."""


def record(
    conn: sqlite3.Connection,
    known_events: set[str],
    actor: str,
    event: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    outcome: str = "success",
    details: dict[str, str | int | bool | None] | None = None,
) -> None:
    """Append one audit entry; `outcome` is "success", "denied" or "failure"."""
    if event not in known_events:
        raise UnknownAuditEvent(event)
    conn.execute(
        "INSERT INTO audit_log (at, actor, event, object_type, object_id, outcome, details)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).isoformat(),
            actor,
            event,
            object_type,
            object_id,
            outcome,
            json.dumps(details or {}),
        ),
    )
    conn.commit()
