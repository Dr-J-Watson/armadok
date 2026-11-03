"""Audit log: who did what, on which object, with what outcome.

Every action writes one entry, whether it comes from the interface or an API
key. Event types must be declared by a module, so the catalogue of events stays
complete. Never put secret values or note contents in `details`.
"""

from __future__ import annotations

import hashlib
import hmac
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


# Each entry's `entry_hash` covers its own content and the previous entry's hash, signed (HMAC)
# with a key kept outside the database. Editing, inserting or deleting a row in the middle breaks
# every hash after it, and without the key the chain cannot be recomputed. A signed "head" (last
# id and hash) detects entries removed from the end. Rows are also protected from SQL slips by
# triggers (append-only). Without a configured key the hashes are plain SHA-256: they still catch
# accidents but NOT someone who can write to the database file, and `verify_chain` says so.
CHAIN_MIGRATION = Migration(
    "core-0003-audit-chain",
    "ALTER TABLE audit_log ADD COLUMN entry_hash TEXT;",
)

HARDENING_MIGRATION = Migration(
    "core-0004-audit-hardening",
    """
    CREATE INDEX audit_log_object ON audit_log (object_type, object_id, id);
    CREATE TABLE audit_head (id INTEGER PRIMARY KEY CHECK (id = 1), last_id INTEGER NOT NULL,
                             last_hash TEXT NOT NULL, mac TEXT NOT NULL);
    -- A recorded entry never changes: only the hash of a brand-new row may be filled in.
    CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    WHEN OLD.entry_hash IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
    CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
    """,
)

MAX_DETAIL_VALUE = 200
MAX_DETAILS_BYTES = 2048


def _entry_hash(key: bytes | None, previous: str, row: tuple) -> str:
    """Hash one entry together with the previous hash (HMAC when a key is available)."""
    payload = previous.encode() + b"\x00" + json.dumps(row, separators=(",", ":")).encode()
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _head_mac(key: bytes | None, last_id: int, last_hash: str) -> str:
    return _entry_hash(key, "head", (last_id, last_hash))


def _bounded(details: dict[str, str | int | bool | None] | None) -> str:
    """Cap detail values and the whole encoded blob, so a huge value cannot bloat the log."""
    clipped = {
        k[:60]: (v[:MAX_DETAIL_VALUE] if isinstance(v, str) else v)
        for k, v in (details or {}).items()
    }
    encoded = json.dumps(clipped)
    return encoded if len(encoded) <= MAX_DETAILS_BYTES else json.dumps({"truncated": True})


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
    commit: bool = True,
) -> None:
    """Append one audit entry; `outcome` is "success", "denied" or "failure".

    With `commit=False` the caller commits (for an entry that must land in the same transaction
    as the action it records).
    """
    if event not in known_events:
        raise UnknownAuditEvent(event)
    at = datetime.now(UTC).isoformat()
    encoded = _bounded(details)
    key = getattr(conn, "audit_key", None)
    # One writer at a time: the chain needs to read the last hash and add the next atomically.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        previous = conn.execute(
            "SELECT entry_hash FROM audit_log WHERE entry_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cursor = conn.execute(
            "INSERT INTO audit_log (at, actor, event, object_type, object_id, outcome, details)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, actor, event, object_type, object_id, outcome, encoded),
        )
        row = (cursor.lastrowid, at, actor, event, object_type, object_id, outcome, encoded)
        entry = _entry_hash(key, previous["entry_hash"] if previous else "", row)
        conn.execute("UPDATE audit_log SET entry_hash = ? WHERE id = ?", (entry, cursor.lastrowid))
        conn.execute(
            "INSERT INTO audit_head (id, last_id, last_hash, mac) VALUES (1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_id = excluded.last_id,"
            " last_hash = excluded.last_hash, mac = excluded.mac",
            (cursor.lastrowid, entry, _head_mac(key, cursor.lastrowid, entry)),
        )
    except BaseException:
        # Never leave the write lock held by a half-finished entry.
        conn.rollback()
        raise
    if commit:
        conn.commit()


def verify_chain(conn: sqlite3.Connection) -> dict[str, int | bool | None]:
    """Check the chain, the absence of gaps and the signed head.

    Returns `{intact, signed, checked, first_bad_id, legacy_unsigned}`. `signed` is False when
    no key is configured: the result then proves nothing against someone who can write to the
    database. Rows from before the chain existed (no hash, all at the very start) are counted in
    `legacy_unsigned` and skipped.
    """
    key = getattr(conn, "audit_key", None)
    previous = ""
    checked = legacy = 0
    last_id: int | None = None
    started = False

    def result(intact: bool, bad: int | None = None) -> dict[str, int | bool | None]:
        return {
            "intact": intact,
            "signed": key is not None,
            "checked": checked,
            "first_bad_id": bad,
            "legacy_unsigned": legacy,
        }

    rows = conn.execute(
        "SELECT id, at, actor, event, object_type, object_id, outcome, details, entry_hash"
        " FROM audit_log ORDER BY id"
    )
    for r in rows:
        if r["entry_hash"] is None and not started:
            legacy += 1
            last_id = r["id"]
            continue
        started = True
        if last_id is not None and r["id"] != last_id + 1:
            return result(False, r["id"])  # an entry is missing before this one
        if r["entry_hash"] != _entry_hash(key, previous, tuple(r)[:8]):
            return result(False, r["id"])
        previous, last_id = r["entry_hash"], r["id"]
        checked += 1
    head = conn.execute("SELECT * FROM audit_head").fetchone()
    if head is None:
        # No head is only normal when nothing was recorded since the chain was introduced.
        return result(checked == 0)
    if (
        head["mac"] != _head_mac(key, head["last_id"], head["last_hash"])
        or head["last_id"] != last_id
        or head["last_hash"] != previous
    ):
        return result(False, head["last_id"])  # newest entries removed, or the head forged
    return result(True)
