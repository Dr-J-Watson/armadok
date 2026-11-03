"""Database access for users, sessions and the one-time install code."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from armadok.core.db import Migration
from armadok.modules.users.security import new_token, token_hash

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=30)

MIGRATION = Migration(
    "users-0001-create",
    """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    -- One row per login. `family` groups a refresh chain so reuse of an old token can
    -- revoke the whole chain. Access and refresh tokens are stored as hashes only.
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users (id),
        family TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        revoked INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE setup_code (hash TEXT NOT NULL);
    """,
)

THROTTLE_MIGRATION = Migration(
    "users-0002-login-failures",
    """
    CREATE TABLE login_failures (username TEXT NOT NULL COLLATE NOCASE, at TEXT NOT NULL);
    CREATE INDEX login_failures_username ON login_failures (username);
    """,
)

TOTP_MIGRATION = Migration(
    "users-0003-totp",
    """
    CREATE TABLE user_totp (
        user_id TEXT PRIMARY KEY REFERENCES users (id),
        secret_enc TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        last_step INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE backup_codes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users (id),
        code_hash TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX backup_codes_user ON backup_codes (user_id);
    """,
)

MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)


def recent_failures(conn: sqlite3.Connection, username: str) -> int:
    """Count failed logins for a username in the last window."""
    since = (_now() - FAILURE_WINDOW).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE username = ? AND at > ?", (username, since)
    ).fetchone()[0]


def record_failure(conn: sqlite3.Connection, username: str) -> None:
    """Remember a failed login and drop entries older than the window."""
    now = _now()
    conn.execute("DELETE FROM login_failures WHERE at <= ?", ((now - FAILURE_WINDOW).isoformat(),))
    conn.execute(
        "INSERT INTO login_failures (username, at) VALUES (?, ?)", (username, now.isoformat())
    )
    conn.commit()


def clear_failures(conn: sqlite3.Connection, username: str) -> None:
    """Forget failures after a successful login."""
    conn.execute("DELETE FROM login_failures WHERE username = ?", (username,))
    conn.commit()


def _now() -> datetime:
    return datetime.now(UTC)


def count_users(conn: sqlite3.Connection) -> int:
    """Return the number of accounts."""
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(
    conn: sqlite3.Connection, username: str, password_hash: str, *, commit: bool = True
) -> dict:
    """Insert a user and return it (without the password hash)."""
    user_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (user_id, username, password_hash, _now().isoformat()),
    )
    if commit:
        conn.commit()
    return get_user(conn, user_id)  # type: ignore[return-value]


def get_user(conn: sqlite3.Connection, user_id: str) -> dict | None:
    """Return a user without the password hash, or None."""
    row = conn.execute(
        "SELECT id, username, is_active, created_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _public(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Return all users without password hashes."""
    rows = conn.execute("SELECT id, username, is_active, created_at FROM users ORDER BY username")
    return [_public(r) for r in rows]


def _public(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["is_active"] = bool(data["is_active"])
    return data


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    """Return the full row (with password hash) for login checks."""
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def set_active(conn: sqlite3.Connection, user_id: str, active: bool) -> None:
    """Enable or disable an account; disabling also ends all its sessions at once."""
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(active), user_id))
    if not active:
        conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()


def set_password(conn: sqlite3.Connection, user_id: str, password_hash: str) -> None:
    """Change a password and end every session of the account."""
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()


# --- install code -------------------------------------------------------------------------


def store_setup_code(conn: sqlite3.Connection, code_hash: str) -> None:
    """Replace the stored install-code hash."""
    conn.execute("DELETE FROM setup_code")
    conn.execute("INSERT INTO setup_code (hash) VALUES (?)", (code_hash,))
    conn.commit()


def get_setup_code_hash(conn: sqlite3.Connection) -> str | None:
    """Return the install-code hash, or None once setup is done."""
    row = conn.execute("SELECT hash FROM setup_code").fetchone()
    return row["hash"] if row else None


def delete_setup_code(conn: sqlite3.Connection, code_hash: str) -> bool:
    """Delete the install code (no commit); True only for the caller that removed it."""
    cur = conn.execute("DELETE FROM setup_code WHERE hash = ?", (code_hash,))
    return cur.rowcount == 1


# --- sessions -----------------------------------------------------------------------------


def issue_tokens(conn: sqlite3.Connection, user_id: str, family: str | None = None) -> dict:
    """Create an access token and a refresh token in one family; return both in clear.

    The clear tokens are returned once; only their hashes are stored.
    """
    family = family or uuid.uuid4().hex
    now = _now()
    result: dict[str, str] = {"family": family}
    for kind, ttl in (("access", ACCESS_TTL), ("refresh", REFRESH_TTL)):
        token, hashed = new_token()
        conn.execute(
            "INSERT INTO sessions (id, user_id, family, kind, token_hash, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, user_id, family, kind, hashed, (now + ttl).isoformat()),
        )
        result[kind] = token
    conn.commit()
    return result


def user_for_access_token(conn: sqlite3.Connection, token: str) -> str | None:
    """Return the user id for a valid access token, or None."""
    row = conn.execute(
        "SELECT s.user_id, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND s.kind = 'access' AND s.revoked = 0 AND u.is_active = 1",
        (token_hash(token),),
    ).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    return row["user_id"]


def rotate_refresh(conn: sqlite3.Connection, token: str) -> dict | None:
    """Exchange a refresh token for a new pair; return None if it is not acceptable.

    A refresh token can be used once. Presenting an already used one means it was
    copied, so every session of that user is revoked.
    """
    row = conn.execute(
        "SELECT s.*, u.is_active FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND s.kind = 'refresh'",
        (token_hash(token),),
    ).fetchone()
    if row is None or row["revoked"] or not row["is_active"]:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    # Claim the token with one atomic statement: of two simultaneous requests carrying the
    # same token, only one flips `used` from 0 to 1. The other is treated as a replay.
    claimed = conn.execute("UPDATE sessions SET used = 1 WHERE id = ? AND used = 0", (row["id"],))
    if claimed.rowcount != 1:
        conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (row["user_id"],))
        conn.commit()
        return None
    conn.commit()
    return {**issue_tokens(conn, row["user_id"], row["family"]), "user_id": row["user_id"]}


def revoke_other_sessions(conn: sqlite3.Connection, user_id: str, access_token: str | None) -> None:
    """End every session of the user except the one that owns `access_token`."""
    keep = conn.execute(
        "SELECT family FROM sessions WHERE token_hash = ?",
        (token_hash(access_token or ""),),
    ).fetchone()
    conn.execute(
        "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND family != ?",
        (user_id, keep["family"] if keep else ""),
    )
    conn.commit()


def revoke_family_of_access_token(conn: sqlite3.Connection, token: str) -> None:
    """End the session (access and refresh tokens) that issued this access token."""
    conn.execute(
        "UPDATE sessions SET revoked = 1 WHERE family ="
        " (SELECT family FROM sessions WHERE token_hash = ?)",
        (token_hash(token),),
    )
    conn.commit()


# --- two-factor authentication ------------------------------------------------------------


def get_totp(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    """Return the TOTP row of a user (encrypted secret, confirmed flag, last used step)."""
    return conn.execute("SELECT * FROM user_totp WHERE user_id = ?", (user_id,)).fetchone()


def save_totp(conn: sqlite3.Connection, user_id: str, secret_enc: str) -> bool:
    """Store a new, not yet confirmed TOTP secret; False if 2FA is already confirmed.

    One atomic statement: a confirmed secret is never overwritten, even by a concurrent
    enrolment.
    """
    cur = conn.execute(
        "INSERT INTO user_totp (user_id, secret_enc, confirmed, last_step) VALUES (?, ?, 0, 0)"
        " ON CONFLICT(user_id) DO UPDATE SET secret_enc = excluded.secret_enc, last_step = 0"
        " WHERE user_totp.confirmed = 0",
        (user_id, secret_enc),
    )
    conn.commit()
    return cur.rowcount == 1


def confirm_totp(
    conn: sqlite3.Connection, user_id: str, secret_enc: str, backup_hashes: list[str]
) -> bool:
    """Switch 2FA on for exactly the secret that was verified, and store the backup codes.

    False (and nothing changed) if the secret was replaced or 2FA was already on.
    """
    cur = conn.execute(
        "UPDATE user_totp SET confirmed = 1 WHERE user_id = ? AND confirmed = 0 AND secret_enc = ?",
        (user_id, secret_enc),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return False
    conn.execute("DELETE FROM backup_codes WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO backup_codes (id, user_id, code_hash) VALUES (?, ?, ?)",
        [(uuid.uuid4().hex, user_id, h) for h in backup_hashes],
    )
    conn.commit()
    return True


def disable_totp(conn: sqlite3.Connection, user_id: str) -> None:
    """Remove 2FA and its backup codes."""
    conn.execute("DELETE FROM user_totp WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM backup_codes WHERE user_id = ?", (user_id,))
    conn.commit()


def use_totp_step(conn: sqlite3.Connection, user_id: str, step: int) -> bool:
    """Record a time step as used; False if it (or a later one) was already used.

    One atomic statement, so the same code cannot be accepted twice, even concurrently.
    """
    cur = conn.execute(
        "UPDATE user_totp SET last_step = ? WHERE user_id = ? AND last_step < ?",
        (step, user_id, step),
    )
    conn.commit()
    return cur.rowcount == 1


def use_backup_code(conn: sqlite3.Connection, user_id: str, code_hash: str) -> bool:
    """Burn a backup code; True only if it existed and was unused."""
    cur = conn.execute(
        "UPDATE backup_codes SET used = 1 WHERE user_id = ? AND code_hash = ? AND used = 0",
        (user_id, code_hash),
    )
    conn.commit()
    return cur.rowcount == 1


def remaining_backup_codes(conn: sqlite3.Connection, user_id: str) -> int:
    """Count unused backup codes."""
    return conn.execute(
        "SELECT COUNT(*) FROM backup_codes WHERE user_id = ? AND used = 0", (user_id,)
    ).fetchone()[0]
