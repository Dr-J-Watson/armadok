"""Authentication by API key or by the short-lived OAuth2 token a key obtained.

A key is `Authorization: Bearer arm_<id>_<secret>`; a token (`armt_...`) comes from `oauth.py`.
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import Request

from armadok.core.grants import parse_expiry
from armadok.core.rights import Principal
from armadok.modules.apikeys import repository
from armadok.modules.users.auth import bearer_token
from armadok.modules.users.security import token_hash

_KEY = re.compile(r"^arm_([0-9a-f]{12})_([A-Za-z0-9_-]{20,100})$")
TOKEN_PREFIX = "armt_"


def key_row_if_valid(conn: sqlite3.Connection, key_id: str, secret: str) -> sqlite3.Row | None:
    """Return the key's row if the secret is right and the key is usable, else None.

    A hash is compared even for an unknown key, to keep timing uniform.
    """
    row = repository.get_key_for_auth(conn, key_id)
    stored = row["secret_hash"] if row else token_hash("unknown")
    if not hmac.compare_digest(stored, token_hash(secret)) or row is None:
        return None
    return row if key_is_usable(row) else None


def key_is_usable(row: sqlite3.Row) -> bool:
    """Whether a key may act now: not revoked, not expired, its owner account still active."""
    expires = parse_expiry(row["expires_at"])
    return not (
        row["revoked"] or not row["owner_active"] or (expires and expires <= datetime.now(UTC))
    )


def principal_for_key(
    request: Request, conn: sqlite3.Connection, row: sqlite3.Row
) -> Principal | None:
    """Build the principal of a usable key.

    The key acts as its owner but only with the rights it was created with, and only while the
    owner still holds them: if the owner loses a right, so does the key. For a service account
    the owner's rights may be those of the person responsible for it (see `grants_of`).
    """
    registry = request.app.state.registry
    if registry.account_blocked(conn, row["owner_id"]):
        return None
    allowed = set(json.loads(row["rights"]))
    repository.touch(conn, row["id"])
    full = registry.principal_for(
        conn,
        row["owner_id"],
        kind="api_key",
        key_id=row["id"],
        read_only=bool(row["read_only"]),
    )
    return replace(full, grants=tuple(g for g in full.grants if g.right in allowed))


def authenticate(request: Request) -> Principal | None:
    """Return the principal for a valid key or OAuth2 token, or None."""
    token = bearer_token(request)
    if token is None:
        return None
    if token.startswith(TOKEN_PREFIX):
        return _authenticate_token(request, token)
    match = _KEY.match(token)
    if match is None:
        return None
    conn = request.app.state.settings.open_db()
    try:
        row = key_row_if_valid(conn, *match.groups())
        return principal_for_key(request, conn, row) if row else None
    finally:
        conn.close()


def _authenticate_token(request: Request, token: str) -> Principal | None:
    conn = request.app.state.settings.open_db()
    try:
        found = conn.execute(
            "SELECT key_id, expires_at FROM oauth_tokens WHERE token_hash = ?",
            (token_hash(token),),
        ).fetchone()
        if found is None or datetime.fromisoformat(found["expires_at"]) <= datetime.now(UTC):
            return None
        # The key must still be usable: revoking a key ends the tokens it handed out.
        row = repository.get_key_for_auth(conn, found["key_id"])
        return principal_for_key(request, conn, row) if row and key_is_usable(row) else None
    finally:
        conn.close()
