"""FastAPI dependencies shared by every module's routes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator

from fastapi import Request

from armadok.core.db import connect
from armadok.core.errors import ApiError, Unauthenticated
from armadok.core.registry import Registry
from armadok.core.rights import Principal

# Turns a request into the acting principal, or None when credentials are missing/invalid.
Authenticator = Callable[[Request], Principal | None]


def get_registry(request: Request) -> Registry:
    """Return the module registry built at start-up."""
    return request.app.state.registry


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a database connection for one request and close it afterwards."""
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_principal(request: Request) -> Principal:
    """Return the authenticated principal or raise `Unauthenticated` (401)."""
    authenticator: Authenticator | None = request.app.state.authenticator
    principal = authenticator(request) if authenticator else None
    if principal is None:
        raise Unauthenticated()
    if principal.read_only and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise ApiError("auth.read_only_key", 403)
    return principal
