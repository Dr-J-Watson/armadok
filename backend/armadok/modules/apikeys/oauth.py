"""OAuth2 client credentials, for tools that insist on it.

`POST /api/v1/oauth/token` with `grant_type=client_credentials`: the *client id* is an API key's
id and the *client secret* is the secret part of that key. The answer is a short-lived bearer
token that acts exactly like the key (same rights, same limits, read-only if the key is) and
stops working when the key is revoked. Answers follow RFC 6749 (error bodies are
`{"error": "invalid_client"}`), because standard OAuth2 libraries expect that shape.
"""

from __future__ import annotations

import base64
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from armadok.core import audit
from armadok.core.deps import get_db, get_registry
from armadok.core.registry import Registry
from armadok.core.throttle import FailureLimiter
from armadok.modules.apikeys.auth import TOKEN_PREFIX, key_row_if_valid
from armadok.modules.users.security import token_hash

router = APIRouter(tags=["oauth"])

TOKEN_SECONDS = 3600
MAX_LIVE_TOKENS_PER_KEY = 50
MAX_BODY = 4096
limiter = FailureLimiter({"ip": 20, "client": 20})
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(code: str, status: int, *, challenge: bool = False) -> JSONResponse:
    headers = dict(_HEADERS)
    if challenge:
        headers["WWW-Authenticate"] = 'Basic realm="armadok"'
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _credentials(request: Request, form: dict[str, list[str]]) -> tuple[str, str] | None:
    """Return `(client_id, client_secret)` from HTTP Basic or from the form."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "basic" and value:
        try:
            client_id, _, client_secret = (
                base64.b64decode(value, validate=True).decode().partition(":")
            )
        except ValueError:
            return None
        return client_id, client_secret
    client_id, client_secret = form.get("client_id", [""])[0], form.get("client_secret", [""])[0]
    return (client_id, client_secret) if client_id and client_secret else None


_audited: dict[tuple[str, str], float] = {}


def _audit_once(conn, registry, key_id: str, event: str, owner_id: str | None = None) -> None:
    """Audit at most once a minute per key and kind, so repeated calls cannot flood the log.

    A refusal is recorded only for a key that exists, so guessing cannot invent entries.
    """
    row = conn.execute("SELECT owner_id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if row is None:
        return
    now = time.monotonic()
    if now - _audited.get((key_id, event), -61) < 60:
        return
    _audited[(key_id, event)] = now
    if len(_audited) > 5000:
        _audited.clear()
    audit.record(
        conn,
        registry.audit_events(),
        f"{owner_id or row['owner_id']} via key:{key_id}",
        event,
        object_type="apikey",
        object_id=key_id,
        outcome="success" if event == "oauth.token" else "denied",
    )


@router.post("/oauth/token", summary="Get a short-lived token (client credentials)")
async def token(
    request: Request,
    conn=Depends(get_db),
    registry: Registry = Depends(get_registry),
) -> JSONResponse:
    """Exchange a key id and secret for a bearer token valid for an hour."""
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:  # stop reading: this endpoint is open to anyone
            return _error("invalid_request", 400)
    try:
        form = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=10)
    except ValueError:
        return _error("invalid_request", 400)
    if form.get("grant_type", [""])[0] != "client_credentials":
        return _error("unsupported_grant_type", 400)
    given = _credentials(request, form)
    if given is None:
        return _error("invalid_client", 401, challenge=True)
    client_id, client_secret = given
    source = request.client.host if request.client else ""
    row = key_row_if_valid(conn, client_id, client_secret)
    if row is None:
        # Only wrong secrets are slowed down: the right one is never blocked by guessing.
        if limiter.blocked(ip=source, client=client_id[:40]):
            return _error("invalid_client", 429)
        limiter.remember(ip=source, client=client_id[:40])
        _audit_once(conn, registry, client_id, "oauth.refused")
        return _error("invalid_client", 401, challenge=True)
    value = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now.isoformat(),))
    conn.execute(
        "DELETE FROM oauth_tokens WHERE key_id = ? AND token_hash NOT IN ("
        " SELECT token_hash FROM oauth_tokens WHERE key_id = ? ORDER BY created_at DESC LIMIT ?)",
        (row["id"], row["id"], MAX_LIVE_TOKENS_PER_KEY - 1),
    )
    conn.execute(
        "INSERT INTO oauth_tokens (token_hash, key_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (
            token_hash(value),
            row["id"],
            (now + timedelta(seconds=TOKEN_SECONDS)).isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()
    _audit_once(conn, registry, row["id"], "oauth.token", row["owner_id"])
    return JSONResponse(
        {"access_token": value, "token_type": "Bearer", "expires_in": TOKEN_SECONDS},
        headers=_HEADERS,
    )
