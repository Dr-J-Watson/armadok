"""Web Push, written on top of `cryptography` alone (no extra dependency to keep patched).

Two standards, both small:

- RFC 8291 + RFC 8188 (`aes128gcm`): the message is encrypted for ONE browser with a key only that
  browser can derive, so the push service that carries it (Google, Mozilla, Apple...) sees nothing
  but ciphertext.
- RFC 8292 (VAPID): a signed token that proves the message comes from this Armadok, with a key
  pair that never leaves it.

Pure functions: no network and no database here (see `modules/notifications` for sending).
"""

from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
# One record only: the plaintext, one delimiter byte and the tag must fit in it. The header is
# 86 bytes more, and push services accept bodies up to 4096 bytes.
MAX_PLAINTEXT = RECORD_SIZE - 17 - 1 - 86
VAPID_LIFETIME_SECONDS = 12 * 3600  # the standard allows up to 24 hours


def b64u(data: bytes) -> str:
    """Encode in base64url without padding (the form push standards use)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    """Decode base64url with or without padding."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    sender: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
    pad_to: int = 0,
) -> bytes:
    """Return the body of a push request: the message encrypted for the browser (RFC 8291).

    `ua_public` is the browser's 65-byte P-256 key and `auth_secret` its 16-byte secret, both from
    its subscription. `sender` and `salt` exist for the standard's test vector; in use both are
    fresh and random for every message. `pad_to` makes every message the same length (zeros after
    the delimiter, as the standard allows), so the push service cannot tell short from long.
    """
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError("message too long")
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("bad subscription keys")
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    browser = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_public = _point(sender.public_key())
    shared = sender.exchange(ec.ECDH(), browser)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = plaintext + b"\x02"  # 0x02 ends the last (only) record; zeros after it are padding
    body += b"\x00" * max(0, min(pad_to, MAX_PLAINTEXT + 1) - len(body))
    sealed = AESGCM(key).encrypt(nonce, body, None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + sealed


def new_vapid_key() -> ec.EllipticCurvePrivateKey:
    """Make a VAPID key pair (P-256)."""
    return ec.generate_private_key(ec.SECP256R1())


def vapid_private_pem(key: ec.EllipticCurvePrivateKey) -> str:
    """Serialise the private key to keep it (the caller encrypts it)."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def load_vapid_key(pem: str) -> ec.EllipticCurvePrivateKey:
    """Read a key made by `vapid_private_pem`; anything but a P-256 key is refused."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a P-256 key")
    return key


def vapid_public(key: ec.EllipticCurvePrivateKey) -> str:
    """Return the public key as the browser wants it for `applicationServerKey`."""
    return b64u(_point(key.public_key()))


def vapid_header(
    key: ec.EllipticCurvePrivateKey, endpoint: str, contact: str, now: float | None = None
) -> str:
    """Return the `Authorization` value proving the message comes from this server (RFC 8292)."""
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now if now is not None else time.time()) + VAPID_LIFETIME_SECONDS),
        "sub": contact,
    }
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signature = key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # JWS wants r||s, not DER
    return f"vapid t={head}.{body}.{b64u(raw)}, k={vapid_public(key)}"
