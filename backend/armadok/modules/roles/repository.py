"""Storage of roles, groups and assignments (tables are created by `core.grants`)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core.module import Right
from armadok.modules.roles import builtin


def _now() -> str:
    return datetime.now(UTC).isoformat()


def sync_builtin(conn: sqlite3.Connection, catalogue: dict[str, Right]) -> None:
    """Make the stored built-in roles match the code (run at every start)."""
    for name, rights in builtin.builtin_roles(catalogue).items():
        rid = builtin.role_id(name)
        # A custom role that already uses the name (made before it was reserved) steps aside.
        conn.execute(
            "UPDATE roles SET name = name || ' (custom)' WHERE name = ? AND builtin = 0", (name,)
        )
        conn.execute(
            "INSERT INTO roles (id, name, builtin, created_at) VALUES (?, ?, 1, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (rid, name, _now()),
        )
        conn.execute("DELETE FROM role_rights WHERE role_id = ?", (rid,))
        conn.executemany(
            "INSERT INTO role_rights (role_id, right) VALUES (?, ?)", [(rid, r) for r in rights]
        )
    conn.commit()


def list_roles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every role with its rights."""
    rights: dict[str, list[str]] = {}
    for row in conn.execute("SELECT role_id, right FROM role_rights ORDER BY right"):
        rights.setdefault(row["role_id"], []).append(row["right"])
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "builtin": bool(r["builtin"]),
            "rights": rights.get(r["id"], []),
        }
        for r in conn.execute("SELECT * FROM roles ORDER BY builtin DESC, name")
    ]


def get_role(conn: sqlite3.Connection, role_id: str) -> dict[str, Any] | None:
    """Return one role with its rights, or None."""
    return next((r for r in list_roles(conn) if r["id"] == role_id), None)


def create_role(conn: sqlite3.Connection, name: str, rights: list[str], who: str) -> str:
    """Create a custom role; raise `sqlite3.IntegrityError` if the name is taken."""
    rid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO roles (id, name, builtin, created_by, created_at) VALUES (?, ?, 0, ?, ?)",
        (rid, name, who, _now()),
    )
    conn.executemany(
        "INSERT INTO role_rights (role_id, right) VALUES (?, ?)", [(rid, r) for r in set(rights)]
    )
    conn.commit()
    return rid


def update_role(conn: sqlite3.Connection, role_id: str, name: str, rights: list[str]) -> None:
    """Rename a custom role and replace its rights."""
    conn.execute("UPDATE roles SET name = ? WHERE id = ? AND builtin = 0", (name, role_id))
    conn.execute("DELETE FROM role_rights WHERE role_id = ?", (role_id,))
    conn.executemany(
        "INSERT INTO role_rights (role_id, right) VALUES (?, ?)",
        [(role_id, r) for r in set(rights)],
    )
    conn.commit()


def role_in_use(conn: sqlite3.Connection, role_id: str) -> bool:
    """Return True if any assignment uses the role."""
    return (
        conn.execute("SELECT 1 FROM assignments WHERE role_id = ?", (role_id,)).fetchone()
        is not None
    )


def delete_role(conn: sqlite3.Connection, role_id: str) -> None:
    """Delete a custom role."""
    conn.execute("DELETE FROM role_rights WHERE role_id = ?", (role_id,))
    conn.execute("DELETE FROM roles WHERE id = ? AND builtin = 0", (role_id,))
    conn.commit()


# --- groups ----------------------------------------------------------------------------------


def list_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every group with its member ids."""
    members: dict[str, list[str]] = {}
    for row in conn.execute("SELECT group_id, user_id FROM group_members ORDER BY user_id"):
        members.setdefault(row["group_id"], []).append(row["user_id"])
    return [
        {"id": g["id"], "name": g["name"], "members": members.get(g["id"], [])}
        for g in conn.execute("SELECT * FROM groups ORDER BY name")
    ]


def get_group(conn: sqlite3.Connection, group_id: str) -> dict[str, Any] | None:
    """Return one group, or None."""
    return next((g for g in list_groups(conn) if g["id"] == group_id), None)


def create_group(conn: sqlite3.Connection, name: str) -> str:
    """Create a group; raise `sqlite3.IntegrityError` if the name is taken."""
    gid = uuid.uuid4().hex
    conn.execute("INSERT INTO groups (id, name, created_at) VALUES (?, ?, ?)", (gid, name, _now()))
    conn.commit()
    return gid


def delete_group(conn: sqlite3.Connection, group_id: str) -> None:
    """Delete a group with its memberships and the assignments made to it."""
    conn.execute(
        "DELETE FROM assignments WHERE subject_type = 'group' AND subject_id = ?", (group_id,)
    )
    conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()


def add_member(conn: sqlite3.Connection, group_id: str, user_id: str) -> None:
    """Put a user in a group (already a member is fine)."""
    conn.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)", (group_id, user_id)
    )
    conn.commit()


def remove_member(conn: sqlite3.Connection, group_id: str, user_id: str) -> bool:
    """Take a user out of a group; return False if they were not in it."""
    cur = conn.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


# --- assignments -----------------------------------------------------------------------------


def list_assignments(
    conn: sqlite3.Connection, subject_type: str | None = None, subject_id: str | None = None
) -> list[dict[str, Any]]:
    """Return assignments (optionally of one subject) with role and subject names."""
    sql = (
        "SELECT a.*, r.name AS role_name, r.builtin AS role_builtin,"
        " CASE a.subject_type WHEN 'user' THEN u.username ELSE g.name END AS subject_name"
        " FROM assignments a JOIN roles r ON r.id = a.role_id"
        " LEFT JOIN users u ON a.subject_type = 'user' AND u.id = a.subject_id"
        " LEFT JOIN groups g ON a.subject_type = 'group' AND g.id = a.subject_id"
    )
    args: list[str] = []
    if subject_type and subject_id:
        sql += " WHERE a.subject_type = ? AND a.subject_id = ?"
        args = [subject_type, subject_id]
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.created_at, a.id", args)]


def get_assignment(conn: sqlite3.Connection, assignment_id: str) -> dict[str, Any] | None:
    """Return one assignment, or None."""
    return next((a for a in list_assignments(conn) if a["id"] == assignment_id), None)


def add_assignment(
    conn: sqlite3.Connection,
    subject_type: str,
    subject_id: str,
    role_id: str,
    scope: str,
    expires_at: datetime | None,
    who: str,
) -> str:
    """Store an assignment and return its id."""
    aid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO assignments (id, subject_type, subject_id, role_id, scope, expires_at,"
        " granted_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            aid,
            subject_type,
            subject_id,
            role_id,
            scope,
            expires_at.isoformat() if expires_at else None,
            who,
            _now(),
        ),
    )
    conn.commit()
    return aid


def remove_assignment(conn: sqlite3.Connection, assignment_id: str) -> None:
    """Delete one assignment."""
    conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
    conn.commit()
