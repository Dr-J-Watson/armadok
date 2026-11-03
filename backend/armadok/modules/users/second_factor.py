"""Checking a second-factor code (authenticator code or backup code) for a user."""

from __future__ import annotations

import re
import sqlite3

from armadok.core import crypto
from armadok.core.config import Settings
from armadok.modules.users import repository, totp
from armadok.modules.users.security import token_hash

_TOTP_CODE = re.compile(r"^\d{6}$")


def is_enabled(conn: sqlite3.Connection, user_id: str) -> bool:
    """Return True if the user has a confirmed second factor."""
    row = repository.get_totp(conn, user_id)
    return bool(row and row["confirmed"])


def verify_totp(conn: sqlite3.Connection, settings: Settings, user_id: str, code: str) -> bool:
    """Check an authenticator code; a time step is accepted only once."""
    row = repository.get_totp(conn, user_id)
    if row is None or not _TOTP_CODE.match(code):
        return False
    step = totp.matching_step(crypto.decrypt(settings, row["secret_enc"]), code)
    return step is not None and repository.use_totp_step(conn, user_id, step)


def verify(conn: sqlite3.Connection, settings: Settings, user_id: str, code: str) -> bool:
    """Accept an authenticator code or an unused backup code (burned on use)."""
    code = code.strip()
    if _TOTP_CODE.match(code):
        return verify_totp(conn, settings, user_id, code)
    return repository.use_backup_code(conn, user_id, token_hash(code.lower()))
