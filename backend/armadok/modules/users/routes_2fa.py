"""Two-factor authentication endpoints: enrol, confirm, disable."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from armadok.core import audit, crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.users import repository, second_factor, totp
from armadok.modules.users.security import token_hash, verify_password

router = APIRouter(prefix="/auth/2fa", tags=["auth"])


class CodeIn(BaseModel):
    """A code from the authenticator app."""

    code: str = Field(min_length=6, max_length=6)


class DisableIn(BaseModel):
    """Password plus a code (authenticator or backup) to turn 2FA off."""

    password: str = Field(max_length=256)
    code: str = Field(max_length=32)


@router.post("/enroll", summary="Start enrolling an authenticator app")
def enroll(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
) -> dict[str, str]:
    """Create a secret and return it with the `otpauth://` URI for the QR code.

    The secret is shown only here; it is stored encrypted. 2FA is not active until
    `/confirm` succeeds.
    """
    if second_factor.is_enabled(conn, who.id):
        raise ApiError("auth.2fa_already_enabled", 409)
    user = repository.get_user(conn, who.id)
    secret = totp.new_secret()
    repository.save_totp(conn, who.id, crypto.encrypt(request.app.state.settings, secret))
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user["username"])}


@router.post("/confirm", summary="Confirm enrolment with a first code")
def confirm(
    body: CodeIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict[str, list[str]]:
    """Turn 2FA on after a valid code, and return the backup codes (shown once)."""
    row = repository.get_totp(conn, who.id)
    if row is None or row["confirmed"]:
        raise ApiError("auth.2fa_not_enrolling", 409)
    if not second_factor.verify_totp(conn, request.app.state.settings, who.id, body.code):
        raise ApiError("auth.invalid_code", 401)
    codes = totp.new_backup_codes()
    repository.confirm_totp(conn, who.id, [token_hash(c) for c in codes])
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
    """Turn 2FA off. Needs the password and a valid code, so a stolen session is not enough."""
    row = repository.get_user_by_username(conn, repository.get_user(conn, who.id)["username"])
    if not verify_password(body.password, row["password_hash"]) or not second_factor.verify(
        conn, request.app.state.settings, who.id, body.code
    ):
        raise ApiError("auth.invalid_credentials", 401)
    repository.disable_totp(conn, who.id)
    audit.record(conn, registry.audit_events(), who.id, "auth.2fa.disable")
