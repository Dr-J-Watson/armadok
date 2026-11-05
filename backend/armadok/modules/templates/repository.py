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

SHARING_MIGRATION = Migration(
    "templates-0002-sharing",
    """
    ALTER TABLE project_templates ADD COLUMN shared_all INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE template_shares (
        template_id TEXT NOT NULL REFERENCES project_templates(id) ON DELETE CASCADE,
        group_id TEXT NOT NULL,
        PRIMARY KEY (template_id, group_id)
    );
    CREATE INDEX template_shares_group ON template_shares (group_id);
    """,
)

MAX_PER_OWNER = 200
MAX_SHARED_GROUPS = 20

# A template can be used by its owner, by everybody it is shared with, or by a member of a group
# it is shared with. Only the owner changes or deletes it.
_VISIBLE = (
    "(t.owner_id = :me OR t.shared_all = 1 OR t.id IN (SELECT s.template_id FROM template_shares s"
    " JOIN group_members m ON m.group_id = s.group_id WHERE m.user_id = :me))"
)


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


def list_for(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Return the templates the account may use (name and description, not the text)."""
    rows = conn.execute(
        "SELECT t.id, t.name, t.description, t.created_at, t.shared_all,"
        " t.owner_id = :me AS mine, u.username AS owner_name FROM project_templates t"
        f" LEFT JOIN users u ON u.id = t.owner_id WHERE {_VISIBLE}"
        " ORDER BY t.name COLLATE NOCASE",
        {"me": user_id},
    ).fetchall()
    out = []
    for r in rows:
        item = {**dict(r), "mine": bool(r["mine"]), "shared_all": bool(r["shared_all"])}
        item["groups"] = (
            [
                g["group_id"]
                for g in conn.execute(
                    "SELECT group_id FROM template_shares WHERE template_id = ?", (r["id"],)
                )
            ]
            if item["mine"]
            else []
        )
        if item["mine"]:
            item["owner_name"] = None
        out.append(item)
    return out


def get(
    conn: sqlite3.Connection, settings: Settings, user_id: str, template_id: str
) -> dict[str, Any] | None:
    """Return one usable template with its Compose text, or None if it is not visible."""
    row = conn.execute(
        "SELECT t.id, t.name, t.description, t.compose, t.encrypted FROM project_templates t"
        f" WHERE t.id = :id AND {_VISIBLE}",
        {"id": template_id, "me": user_id},
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


def my_groups(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """Return the groups the account belongs to (the only ones it may share with)."""
    rows = conn.execute(
        "SELECT g.id, g.name FROM groups g JOIN group_members m ON m.group_id = g.id"
        " WHERE m.user_id = ? ORDER BY g.name COLLATE NOCASE",
        (user_id,),
    )
    return [dict(r) for r in rows]


def set_sharing(
    conn: sqlite3.Connection, owner_id: str, template_id: str, everyone: bool, groups: list[str]
) -> bool:
    """Set who a template is shared with; False if it is not the account's own."""
    if not conn.execute(
        "SELECT 1 FROM project_templates WHERE id = ? AND owner_id = ?", (template_id, owner_id)
    ).fetchone():
        return False
    conn.execute(
        "UPDATE project_templates SET shared_all = ? WHERE id = ?", (int(everyone), template_id)
    )
    conn.execute("DELETE FROM template_shares WHERE template_id = ?", (template_id,))
    conn.executemany(
        "INSERT INTO template_shares (template_id, group_id) VALUES (?, ?)",
        [(template_id, g) for g in dict.fromkeys(groups)],
    )
    conn.commit()
    return True


def delete(conn: sqlite3.Connection, owner_id: str, template_id: str) -> bool:
    """Delete one of the account's templates; False if there was none."""
    done = conn.execute(
        "DELETE FROM project_templates WHERE id = ? AND owner_id = ?", (template_id, owner_id)
    ).rowcount
    conn.commit()
    return bool(done)
