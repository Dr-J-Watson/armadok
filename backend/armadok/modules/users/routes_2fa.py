"""Two-factor authentication endpoints: enrol, confirm, disable.

All of them need a real session (not an API key) and count failed attempts, so neither
the password nor a 6-digit code can be guessed here without limit.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from armadok.core import audit, crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.users import repository, second_factor, totp
from armadok.modules.users.auth import bearer_token
from armadok.modules.users.security import token_hash, verify_password

router = APIRouter(prefix="/auth/2fa", tags=["auth"])


class EnrollIn(BaseModel):
    """The current password, asked again before touching the second factor."""

    password: str = Field(max_length=256)


class CodeIn(BaseModel):
    """A code from the authenticator app."""

    code: str = Field(min_length=6, max_length=6)


class DisableIn(BaseModel):
    """Password plus a code (authenticator or backup) to turn 2FA off."""

    password: str = Field(max_length=256)
    code: str = Field(max_length=32)


def _session_only(who: Principal) -> None:
    """Keys cannot touch the second factor of the account they act for."""
    if who.kind != "session":
        raise MissingRight("auth.not_with_key")


def _guard(conn: sqlite3.Connection, who: Principal) -> str:
    """Refuse (429) after too many recent failures on this account's 2FA screens."""
    key = f"2fa:{who.id}"
    if repository.recent_failures(conn, key) >= repository.MAX_FAILURES:
        raise ApiError("auth.too_many_attempts", 429)
    return key


def _password_ok(conn: sqlite3.Connection, who: Principal, password: str) -> bool:
    row = repository.get_user_by_username(conn, repository.get_user(conn, who.id)["username"])
    return verify_password(password, row["password_hash"])


@router.post("/enroll", summary="Start enrolling an authenticator app")
def enroll(
    body: EnrollIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> dict[str, str]:
    """Create a secret and return it with the `otpauth://` URI for the QR code.

    Needs the password, so a stolen session alone cannot take over the second factor.
    The secret is shown only here and stored encrypted; 2FA is not active until
    `/confirm` succeeds.
    """
    _session_only(who)
    key = _guard(conn, who)
    if not _password_ok(conn, who, body.password):
        repository.record_failure(conn, key)
        raise ApiError("auth.invalid_credentials", 401)
    secret = totp.new_secret()
    if not repository.save_totp(conn, who.id, crypto.encrypt(request.app.state.settings, secret)):
        raise ApiError("auth.2fa_already_enabled", 409)
    user = repository.get_user(conn, who.id)
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user["username"])}


@router.post("/confirm", summary="Confirm enrolment with a first code")
def confirm(
    body: CodeIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, list[str]]:
    """Turn 2FA on after a valid code; return the backup codes (shown once).

    Every other session of the account is ended, so a session opened before 2FA existed
    cannot outlive the change.
    """
    _session_only(who)
    key = _guard(conn, who)
    row = repository.get_totp(conn, who.id)
    if row is None or row["confirmed"]:
        raise ApiError("auth.2fa_not_enrolling", 409)
    if not second_factor.verify_totp(conn, request.app.state.settings, who.id, body.code):
        repository.record_failure(conn, key)
        raise ApiError("auth.invalid_code", 401)
    codes = totp.new_backup_codes()
    if not repository.confirm_totp(conn, who.id, row["secret_enc"], [token_hash(c) for c in codes]):
        raise ApiError("auth.2fa_not_enrolling", 409)
    repository.clear_failures(conn, key)
    repository.revoke_other_sessions(conn, who.id, bearer_token(request))
    audit.record(conn, registry.audit_events(), who.id, "auth.2fa.enable")
    return {"backup_codes": codes}


@router.post("/disable", status_code=204, summary="Turn 2FA off")
def disable(
    body: DisableIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Turn 2FA off. Needs the password and a valid code; other sessions are ended."""
    _session_only(who)
    key = _guard(conn, who)
    # The code is only checked (and a backup code only burned) once the password is right.
    password_ok = _password_ok(conn, who, body.password)
    code_ok = password_ok and second_factor.verify(
        conn, request.app.state.settings, who.id, body.code
    )
    if not code_ok:
        repository.record_failure(conn, key)
        audit.record(conn, registry.audit_events(), who.id, "auth.2fa.disable", outcome="denied")
        raise ApiError("auth.invalid_credentials", 401)
    repository.clear_failures(conn, key)
    repository.disable_totp(conn, who.id)
    repository.revoke_other_sessions(conn, who.id, bearer_token(request))
    audit.record(conn, registry.audit_events(), who.id, "auth.2fa.disable")
