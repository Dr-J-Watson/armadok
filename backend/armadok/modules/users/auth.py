"""Request authentication: turns a Bearer access token into a `Principal`."""

from __future__ import annotations

from fastapi import Request

from armadok.core.rights import Principal
from armadok.modules.users import repository


def bearer_token(request: Request) -> str | None:
    """Return the token from `Authorization: Bearer ...`, or None.

    Tokens are accepted from the header only, never from the URL (URLs end up in logs).
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def authenticate(request: Request) -> Principal | None:
    """Return the principal for a valid access token, or None."""
    token = bearer_token(request)
    if token is None:
        return None
    conn = request.app.state.settings.open_db()
    try:
        user_id = repository.user_for_access_token(conn, token)
        if user_id is None:
            return None
        return request.app.state.registry.principal_for(conn, user_id)
    finally:
        conn.close()
