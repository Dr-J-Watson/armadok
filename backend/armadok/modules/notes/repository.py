"""Database access for notes.

Every query is scoped to the owner: there is no way to read another account's note, not even
for an administrator.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from armadok.core import crypto
from armadok.core.config import Settings
from armadok.core.db import Migration
from armadok.core.errors import ApiError

MIGRATION = Migration(
    "notes-0001-create",
    """
    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        body TEXT NOT NULL,
        encrypted INTEGER NOT NULL DEFAULT 0,
        importance TEXT NOT NULL DEFAULT 'info',
        pinned INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0,
        remind_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX notes_owner ON notes (owner_id, node_id, object_type, object_id);
    """,
)

REMINDER_MIGRATION = Migration(
    "notes-0002-reminded",
    "ALTER TABLE notes ADD COLUMN reminded_at TEXT;",
)

SHARES_MIGRATION = Migration(
    "notes-0003-shares",
    """
    CREATE TABLE note_shares (
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (note_id, user_id)
    );
    CREATE INDEX note_shares_user ON note_shares (user_id);
    """,
)

MAX_PER_OWNER = 5000  # bounds the in-memory search and the table size per account


def _seal(settings: Settings, text: str) -> tuple[str, int]:
    """Encrypt a note's text when a key is configured, so a database copy does not read it."""
    if settings.encryption_key:
        return crypto.encrypt(settings, text), 1
    return text, 0


def _public(settings: Settings, row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["body"] = crypto.decrypt(settings, data["body"]) if data.pop("encrypted") else data["body"]
    for flag in ("pinned", "done"):
        data[flag] = bool(data[flag])
    data.pop("owner_id")
    data.pop("reminded_at", None)
    if data.get("author"):
        data.pop("remind_at", None)  # a reminder is the author's own
    return data


def _now() -> str:
    return datetime.now(UTC).isoformat()


def count(conn: sqlite3.Connection, owner_id: str) -> int:
    """Return how many notes an account has."""
    return conn.execute("SELECT COUNT(*) FROM notes WHERE owner_id = ?", (owner_id,)).fetchone()[0]


def create(
    conn: sqlite3.Connection,
    settings: Settings,
    owner_id: str,
    node_id: str,
    object_type: str,
    object_id: str,
    body: str,
    importance: str,
    remind_at: str | None,
) -> dict[str, Any]:
    """Insert a note owned by `owner_id` and return it."""
    sealed, encrypted = _seal(settings, body)
    note_id, now = uuid.uuid4().hex, _now()
    conn.execute(
        "INSERT INTO notes (id, owner_id, node_id, object_type, object_id, body, encrypted,"
        " importance, remind_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            note_id,
            owner_id,
            node_id,
            object_type,
            object_id,
            sealed,
            encrypted,
            importance,
            remind_at,
            now,
            now,
        ),
    )
    conn.commit()
    return get(conn, settings, owner_id, note_id)  # type: ignore[return-value]


def get(conn: sqlite3.Connection, settings: Settings, owner_id: str, note_id: str) -> dict | None:
    """Return one of the owner's notes, or None (also for someone else's note)."""
    row = conn.execute(
        "SELECT * FROM notes WHERE id = ? AND owner_id = ?", (note_id, owner_id)
    ).fetchone()
    return _public(settings, row) if row else None


def list_notes(
    conn: sqlite3.Connection,
    settings: Settings,
    owner_id: str,
    *,
    node_id: str | None,
    object_type: str | None,
    object_id: str | None,
    done: bool | None,
) -> list[dict[str, Any]]:
    """Return the owner's notes, pinned first then newest first."""
    where, args = ["owner_id = ?"], [owner_id]
    for column, value in (
        ("node_id", node_id),
        ("object_type", object_type),
        ("object_id", object_id),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            args.append(value)
    if done is not None:
        where.append("done = ?")
        args.append(int(done))
    rows = conn.execute(
        f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY pinned DESC, updated_at DESC"
        " LIMIT ?",
        (*args, MAX_PER_OWNER),
    )
    return _readable(settings, rows)


def _readable(settings: Settings, rows) -> list[dict[str, Any]]:
    """Open each note, skipping one that cannot be read (after a key mix-up, say)."""
    out = []
    for row in rows:
        try:
            out.append(_public(settings, row))
        except ApiError:
            continue
    return out


def update(
    conn: sqlite3.Connection,
    settings: Settings,
    owner_id: str,
    note_id: str,
    changes: dict[str, Any],
) -> None:
    """Apply changes (body, importance, pinned, done, remind_at) to one of the owner's notes."""
    fields: dict[str, Any] = {}
    if "body" in changes:
        fields["body"], fields["encrypted"] = _seal(settings, changes["body"])
    for key in ("importance", "remind_at"):
        if key in changes:
            fields[key] = changes[key]
    if "remind_at" in changes:
        fields["reminded_at"] = None  # a new date means the reminder is due again
    for key in ("pinned", "done"):
        if key in changes:
            fields[key] = int(changes[key])
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE notes SET {assignments} WHERE id = ? AND owner_id = ?",
        (*fields.values(), note_id, owner_id),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, owner_id: str, note_id: str) -> bool:
    """Delete one of the owner's notes; False if it does not exist."""
    cur = conn.execute("DELETE FROM notes WHERE id = ? AND owner_id = ?", (note_id, owner_id))
    conn.commit()
    return cur.rowcount == 1


# --- sharing: a note can be shown, read-only, to people its author names ---------------------

MAX_SHARES_PER_NOTE = 10


def shared_with(conn: sqlite3.Connection, note_id: str) -> list[str]:
    """Return the ids of the people a note is shared with."""
    return [
        r["user_id"]
        for r in conn.execute(
            "SELECT user_id FROM note_shares WHERE note_id = ? ORDER BY created_at", (note_id,)
        )
    ]


def share(conn: sqlite3.Connection, owner_id: str, note_id: str, user_id: str) -> str:
    """Share one of the owner's notes; return "ok", "missing" (not theirs) or "full"."""
    if (
        conn.execute(
            "SELECT 1 FROM notes WHERE id = ? AND owner_id = ?", (note_id, owner_id)
        ).fetchone()
        is None
    ):
        return "missing"
    if len(shared_with(conn, note_id)) >= MAX_SHARES_PER_NOTE:
        return "full"
    conn.execute(
        "INSERT OR IGNORE INTO note_shares (note_id, user_id, created_at) VALUES (?, ?, ?)",
        (note_id, user_id, _now()),
    )
    conn.commit()
    return "ok"


def unshare(conn: sqlite3.Connection, owner_id: str, note_id: str, user_id: str) -> bool:
    """Stop sharing a note with one person; False if it was not shared with them."""
    cur = conn.execute(
        "DELETE FROM note_shares WHERE note_id = ? AND user_id = ? AND note_id IN"
        " (SELECT id FROM notes WHERE owner_id = ?)",
        (note_id, user_id, owner_id),
    )
    conn.commit()
    return cur.rowcount == 1


def shared_to(conn: sqlite3.Connection, settings: Settings, user_id: str) -> list[dict[str, Any]]:
    """Return the notes others shared with this person, newest first.

    The author is named; nothing else about the author's other notes is visible.
    """
    rows = conn.execute(
        "SELECT n.*, u.username AS author FROM notes n"
        " JOIN note_shares s ON s.note_id = n.id JOIN users u ON u.id = n.owner_id"
        " WHERE s.user_id = ? AND u.is_active = 1 ORDER BY n.updated_at DESC LIMIT 500",
        (user_id,),
    )
    out = _readable(settings, rows)
    for note in out:
        note.pop("pinned", None)
        note["read_only"] = True
    return out


def hand_over(conn: sqlite3.Connection, change: Any) -> None:
    """Move the giver's own notes on an object to its new owner, if the giver chose to.

    Notes stay private to one author: this is the author's gift, never a seizure (taking an
    object back never sets `with_notes`). The shares are dropped: the new author chooses afresh.
    Part of the owner change's transaction.
    """
    if not change.with_notes:
        return
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND node_id = ? AND object_type = ?"
            " AND object_id = ?",
            (change.old_owner, change.node_id, change.object_type, change.object_id),
        )
    ]
    if count(conn, change.new_owner) + len(ids) > MAX_PER_OWNER:
        # The receiver's notes are bounded (search keeps them in memory): the gift is refused
        # whole, and the giver can send it again without the notes.
        raise ApiError("notes.too_many", 409)
    for note_id in ids:
        conn.execute("UPDATE notes SET owner_id = ? WHERE id = ?", (change.new_owner, note_id))
        # Who the old author showed it to was their choice, not the new author's.
        conn.execute("DELETE FROM note_shares WHERE note_id = ?", (note_id,))
