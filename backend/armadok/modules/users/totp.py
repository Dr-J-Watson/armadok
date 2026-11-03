"""Time-based one-time passwords (RFC 6238) and single-use backup codes.

Implemented with the standard library: HMAC-SHA1, 30-second steps, 6 digits, the
parameters every authenticator app supports.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
# Accept the previous and next step too, to tolerate small clock differences.
WINDOW = 1
BACKUP_CODE_COUNT = 8


def new_secret() -> str:
    """Return a random base32 secret (160 bits) for an authenticator app."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, step: int) -> str:
    """Return the code for one time step."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    """Return the time step `code` is valid for, or None.

    The caller must refuse a step that was already used, so a code cannot be replayed.
    """
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Armadok") -> str:
    """Return the `otpauth://` URI that authenticator apps read from a QR code."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}"


def new_backup_codes() -> list[str]:
    """Return fresh single-use backup codes (shown once, stored hashed)."""
    return [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(BACKUP_CODE_COUNT)]
