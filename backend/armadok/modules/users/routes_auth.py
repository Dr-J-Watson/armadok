"""Setup and session endpoints: first start, login, refresh, logout, current user."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Cookie, Depends, Request, Response
from pydantic import BaseModel, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.users import repository, second_factor, setup
from armadok.modules.users.auth import bearer_token
from armadok.modules.users.security import (
    DUMMY_HASH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    verify_password,
)

router = APIRouter(tags=["auth"])

REFRESH_COOKIE = "armadok_refresh"
# The refresh cookie is only ever sent to the auth endpoints.
COOKIE_PATH = "/api/v1/auth"

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,64}$"


class SetupIn(BaseModel):
    """Data to create the super-administrator."""

    install_code: str = Field(min_length=1, max_length=200)
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class LoginIn(BaseModel):
    """Credentials."""

    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    # Authenticator code or backup code; required only when the account has 2FA.
    totp_code: str | None = Field(default=None, max_length=32)


class TokenOut(BaseModel):
    """Short-lived access token. The refresh token travels in an HttpOnly cookie."""

    access_token: str
    expires_in: int


def _set_refresh_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=int(repository.REFRESH_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=request.app.state.settings.cookie_secure,
        path=COOKIE_PATH,
    )


def _token_out(request: Request, response: Response, tokens: dict) -> TokenOut:
    _set_refresh_cookie(request, response, tokens["refresh"])
    return TokenOut(
        access_token=tokens["access"], expires_in=int(repository.ACCESS_TTL.total_seconds())
    )


@router.get("/setup/status", summary="Is the first-start setup still open?")
def setup_status(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, bool]:
    """Tell the interface whether to show the setup page."""
    return {"setup_required": repository.count_users(conn) == 0}


@router.post("/setup", status_code=201, summary="Create the super-administrator")
def run_setup(
    body: SetupIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
) -> dict:
    """Create the first account with the install code shown in the server logs.

    Closed for good as soon as an account exists.
    """
    try:
        user = setup.run_setup(
            conn,
            body.install_code,
            body.username,
            hash_password(body.password),
            registry.baseline_rights(),
        )
    except setup.SetupClosed:
        raise ApiError("setup.closed", 409) from None
    if user is None:
        raise ApiError("setup.invalid_code", 403)
    audit.record(
        conn,
        registry.audit_events(),
        user["id"],
        "setup.complete",
        object_type="user",
        object_id=user["id"],
    )
    return user


@router.post("/auth/login", response_model=TokenOut, summary="Log in")
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
) -> TokenOut:
    """Check credentials and open a session.

    The same error is returned for an unknown user, a wrong password and a disabled account.
    """
    # Too many recent failures: refuse before spending CPU on a bcrypt check.
    if repository.recent_failures(conn, body.username) >= repository.MAX_FAILURES:
        raise ApiError("auth.too_many_attempts", 429)
    row = repository.get_user_by_username(conn, body.username)
    ok = verify_password(body.password, row["password_hash"] if row else DUMMY_HASH)
    if row is None or not ok or not row["is_active"]:
        repository.record_failure(conn, body.username)
        audit.record(conn, registry.audit_events(), "anonymous", "auth.login", outcome="denied")
        raise ApiError("auth.invalid_credentials", 401)
    if second_factor.is_enabled(conn, row["id"]):
        if not body.totp_code:
            raise ApiError("auth.totp_required", 401)
        settings = request.app.state.settings
        if not second_factor.verify(conn, settings, row["id"], body.totp_code):
            repository.record_failure(conn, body.username)
            audit.record(conn, registry.audit_events(), row["id"], "auth.login", outcome="denied")
            raise ApiError("auth.invalid_code", 401)
    repository.clear_failures(conn, body.username)
    audit.record(conn, registry.audit_events(), row["id"], "auth.login")
    return _token_out(request, response, repository.issue_tokens(conn, row["id"]))


@router.post("/auth/refresh", response_model=TokenOut, summary="Renew the session")
def refresh(
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
    token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
) -> TokenOut:
    """Exchange the refresh cookie for a new access token and a new cookie."""
    tokens = repository.rotate_refresh(conn, token) if token else None
    if tokens is None:
        response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
        raise ApiError("auth.invalid_session", 401)
    return _token_out(request, response, tokens)


@router.post("/auth/logout", status_code=204, summary="Log out")
def logout(
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """End the current session."""
    token = bearer_token(request)
    if token:
        repository.revoke_family_of_access_token(conn, token)
    audit.record(conn, registry.audit_events(), who.id, "auth.logout")
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)


@router.get("/auth/me", summary="Who am I?")
def me(conn: sqlite3.Connection = Depends(get_db), who: Principal = Depends(get_principal)) -> dict:
    """Return the current account and the rights it holds."""
    user = repository.get_user(conn, who.id)
    return {
        **(user or {}),
        "grants": [
            {"right": g.right, "scope": g.scope, "expires_at": g.expires_at} for g in who.grants
        ],
    }
