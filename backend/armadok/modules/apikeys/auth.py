"""Authenticates a request carrying an API key (`Authorization: Bearer arm_<id>_<secret>`)."""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime

from fastapi import Request

from armadok.core.db import connect
from armadok.core.grants import load_grants, parse_expiry
from armadok.core.rights import Principal
from armadok.modules.apikeys import repository
from armadok.modules.users.auth import bearer_token
from armadok.modules.users.security import token_hash

_KEY = re.compile(r"^arm_([0-9a-f]{12})_([A-Za-z0-9_-]{20,100})$")


def authenticate(request: Request) -> Principal | None:
    """Return the principal for a valid key, or None.

    The key acts as its owner but only with the rights it was created with, and only
    while the owner still holds them: if the owner loses a right, so does the key.
    """
    token = bearer_token(request)
    match = _KEY.match(token) if token else None
    if match is None:
        return None
    key_id, secret = match.groups()
    conn = connect(request.app.state.settings.db_path)
    try:
        row = repository.get_key_for_auth(conn, key_id)
        # Compare against a hash even when the key is unknown, to keep timing uniform.
        stored = row["secret_hash"] if row else token_hash("unknown")
        if not hmac.compare_digest(stored, token_hash(secret)) or row is None:
            return None
        expires = parse_expiry(row["expires_at"])
        if row["revoked"] or not row["owner_active"] or (expires and expires <= datetime.now(UTC)):
            return None
        allowed = set(json.loads(row["rights"]))
        grants = tuple(g for g in load_grants(conn, row["owner_id"]) if g.right in allowed)
        repository.touch(conn, key_id)
        return Principal(
            row["owner_id"],
            grants,
            kind="api_key",
            key_id=key_id,
            read_only=bool(row["read_only"]),
        )
    finally:
        conn.close()
