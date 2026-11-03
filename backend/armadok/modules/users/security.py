"""Password hashing and opaque token helpers.

Passwords use bcrypt on a SHA-256 pre-hash, because bcrypt ignores or rejects
input beyond 72 bytes. Tokens are random and only their SHA-256 is stored, so a
database leak does not reveal usable tokens.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

import bcrypt

MIN_PASSWORD_LENGTH = 12


def _prehash(password: str) -> bytes:
    return base64.b64encode(hashlib.sha256(password.encode("utf-8", "surrogatepass")).digest())


def hash_password(password: str) -> str:
    """Return a bcrypt hash of `password`."""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Return True if `password` matches `hashed`."""
    return bcrypt.checkpw(_prehash(password), hashed.encode())


# A valid hash checked when the user does not exist, so the response time does not
# reveal whether an account exists.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> tuple[str, str]:
    """Return `(token, token_hash)`: give the token to the client, store only the hash."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    """Return the storage hash of a token."""
    return hashlib.sha256(token.encode()).hexdigest()
