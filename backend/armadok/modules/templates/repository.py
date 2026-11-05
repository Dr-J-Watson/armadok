"""Storage of a person's project templates (a Compose text kept to start new projects from)."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core import crypto
from armadok.core.config import Settings
from armadok.core.db import Migration
from armadok.core.errors import ApiError

MIGRATION = Migration(
    "templates-0001-create",
    """
    CREATE TABLE project_templates (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        compose TEXT NOT NULL,
        encrypted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX project_templates_owner ON project_templates (owner_id);
    """,
)

MAX_PER_OWNER = 200


def count(conn: sqlite3.Connection, owner_id: str) -> int:
    """Return how many templates an account has."""
    return conn.execute(
        "SELECT COUNT(*) FROM project_templates WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]


def create(
    conn: sqlite3.Connection,
    settings: Settings,
    owner_id: str,
    name: str,
    description: str,
    compose: str,
) -> dict[str, Any]:
    """Store a template; the Compose text is encrypted when a key is configured."""
    sealed, flag = (
        (crypto.encrypt(settings, compose), 1) if settings.encryption_key else (compose, 0)
    )
    template_id = secrets.token_hex(8)
    conn.execute(
        "INSERT INTO project_templates (id, owner_id, name, description, compose, encrypted,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (template_id, owner_id, name, description, sealed, flag, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return {"id": template_id, "name": name, "description": description}


def list_for(conn: sqlite3.Connection, owner_id: str) -> list[dict[str, Any]]:
    """Return an account's templates (name and description, not the text), by name."""
    rows = conn.execute(
        "SELECT id, name, description, created_at FROM project_templates WHERE owner_id = ?"
        " ORDER BY name COLLATE NOCASE",
        (owner_id,),
    )
    return [dict(r) for r in rows]


def get(
    conn: sqlite3.Connection, settings: Settings, owner_id: str, template_id: str
) -> dict[str, Any] | None:
    """Return one template with its Compose text, or None if it is not this account's."""
    row = conn.execute(
        "SELECT id, name, description, compose, encrypted FROM project_templates"
        " WHERE id = ? AND owner_id = ?",
        (template_id, owner_id),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data.pop("encrypted"):
        try:
            data["compose"] = crypto.decrypt(settings, data["compose"])
        except ApiError:
            raise ApiError("templates.unreadable", 409) from None
    return data


def delete(conn: sqlite3.Connection, owner_id: str, template_id: str) -> bool:
    """Delete one of the account's templates; False if there was none."""
    done = conn.execute(
        "DELETE FROM project_templates WHERE id = ? AND owner_id = ?", (template_id, owner_id)
    ).rowcount
    conn.commit()
    return bool(done)
