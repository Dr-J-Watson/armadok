"""Checking a second-factor code (authenticator code or backup code) for a user."""

from __future__ import annotations

import re
import sqlite3

from armadok.core import crypto
from armadok.core.config import Settings
from armadok.modules.users import repository, totp
from armadok.modules.users.security import token_hash

# ASCII digits only and fullmatch (no trailing newline, no other scripts' digits).
_TOTP_CODE = re.compile(r"[0-9]{6}", re.ASCII)
_BACKUP_CODE = re.compile(r"[0-9a-f]{10}-[0-9a-f]{10}", re.ASCII)


def is_enabled(conn: sqlite3.Connection, user_id: str) -> bool:
    """Return True if the user has a confirmed second factor."""
    row = repository.get_totp(conn, user_id)
    return bool(row and row["confirmed"])


def verify_totp(conn: sqlite3.Connection, settings: Settings, user_id: str, code: str) -> bool:
    """Check an authenticator code; a time step is accepted only once."""
    row = repository.get_totp(conn, user_id)
    if row is None or not _TOTP_CODE.fullmatch(code):
        return False
    step = totp.matching_step(crypto.decrypt(settings, row["secret_enc"]), code)
    return step is not None and repository.use_totp_step(conn, user_id, step)


def verify(conn: sqlite3.Connection, settings: Settings, user_id: str, code: str) -> bool:
    """Accept an authenticator code or an unused backup code (burned on use)."""
    code = code.strip()
    if _TOTP_CODE.fullmatch(code):
        return verify_totp(conn, settings, user_id, code)
    code = code.lower()
    if not _BACKUP_CODE.fullmatch(code):
        return False
    return repository.use_backup_code(conn, user_id, token_hash(code))


def require_fresh_code(
    conn: sqlite3.Connection, settings: Settings, user_id: str, code: str
) -> None:
    """Demand a valid second-factor code for a risky action, or raise.

    The account must have a second factor set up. Wrong codes are counted and, after a few,
    refused for a while (the same limit as at login), so the code cannot be guessed here.
    """
    from armadok.core.errors import ApiError

    if not is_enabled(conn, user_id):
        raise ApiError("auth.2fa_required", 403)
    key = f"action-2fa:{user_id}"
    # The attempt is counted *before* it is checked, so parallel requests cannot all slip
    # under the limit; a success wipes the count.
    repository.record_failure(conn, key)
    if repository.recent_failures(conn, key) > repository.MAX_FAILURES:
        raise ApiError("auth.too_many_attempts", 429)
    if not verify(conn, settings, user_id, code):
        raise ApiError("auth.invalid_code", 401)
    repository.clear_failures(conn, key)
