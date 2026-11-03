"""Encryption of secrets stored in the database.

Uses Fernet (authenticated encryption). The key comes from the environment and is
never stored in the database, so a stolen database file alone reveals no secret.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from armadok.core.config import Settings
from armadok.core.errors import ApiError


def generate_key() -> str:
    """Return a new random key, suitable for `ARMADOK_ENCRYPTION_KEY`."""
    return Fernet.generate_key().decode()


def _fernet(settings: Settings) -> Fernet:
    if not settings.encryption_key:
        # A server error, not the caller's fault: the operator must configure the key.
        raise ApiError("crypto.key_missing", 503)
    try:
        return Fernet(settings.encryption_key.encode())
    except ValueError:
        raise ApiError("crypto.key_invalid", 503) from None


def encrypt(settings: Settings, plaintext: str) -> str:
    """Encrypt a secret for storage."""
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt(settings: Settings, token: str) -> str:
    """Decrypt a stored secret; raise a 503 error if the key is wrong."""
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken:
        raise ApiError("crypto.key_invalid", 503) from None
