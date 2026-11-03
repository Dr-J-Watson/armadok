"""Handshake and framing of the hub <-> worker channel (version 1).

Design (a Noise-style handshake built from standard primitives):

1. The worker sends a hello: an ephemeral X25519 key, a random nonce and the id it claims.
2. The hub answers with its own ephemeral key and nonce, plus an Ed25519 signature of the
   transcript made with its long-term key. The worker checks it against the hub key it has
   **pinned**, so a man in the middle cannot pose as the hub.
3. Both derive two session keys (one per direction) from the X25519 secret with HKDF,
   salted with the transcript hash. Keys are fresh for every connection.
4. Everything after that is ChaCha20-Poly1305 with a strict per-direction counter used as
   the nonce: a replayed, dropped, reordered or altered frame fails to decrypt and the
   channel is closed. The worker proves its identity inside the encrypted channel, so
   it reveals nothing before the hub has been authenticated.

The enrolment token secret never travels: the worker signs the transcript with a key derived
from it. The signature is bound to this very connection, so it cannot be replayed, and the hub
keeps only the public key, so a stolen database cannot enrol anyone.
"""

from __future__ import annotations

import hashlib
import os
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL = b"armadok-w1"
# Frames are small control messages; a cap stops a peer from exhausting memory.
MAX_FRAME_PLAINTEXT = 1024 * 1024
MAX_WORKER_ID_LENGTH = 64
HELLO_FIXED_LENGTH = 3 + 32 + 32 + 1
SERVER_HELLO_LENGTH = 32 + 32 + 64


class ProtocolError(Exception):
    """Raised when a peer breaks the protocol (bad signature, bad frame, replay...)."""


def _raw(public_key: X25519PublicKey | Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    """Return the 32 raw bytes of an Ed25519 public key (what gets pinned and registered)."""
    return _raw(private_key.public_key())


def load_ed25519_private(raw: bytes) -> Ed25519PrivateKey:
    """Rebuild an Ed25519 private key from its 32 raw bytes."""
    return Ed25519PrivateKey.from_private_bytes(raw)


def private_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    """Return the 32 raw bytes of an Ed25519 private key, for storage by the owner only."""
    return private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _transcript_hash(client_hello: bytes, hub_eph: bytes, hub_nonce: bytes) -> bytes:
    return hashlib.sha256(PROTOCOL + client_hello + hub_eph + hub_nonce).digest()


def _session_keys(shared: bytes, transcript: bytes) -> tuple[bytes, bytes]:
    """Derive `(worker_to_hub, hub_to_worker)` keys; the transcript salts the derivation."""
    keys = []
    for label in (b"w2h", b"h2w"):
        keys.append(
            HKDF(
                algorithm=hashes.SHA256(), length=32, salt=transcript, info=PROTOCOL + b" " + label
            ).derive(shared)
        )
    return keys[0], keys[1]


class Channel:
    """One direction pair of encrypted frames with strict counters.

    `seal` and `open` are not thread-safe on purpose: one connection, one owner.
    """

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        """Create a channel from the sending and receiving keys."""
        self._send = ChaCha20Poly1305(send_key)
        self._recv = ChaCha20Poly1305(recv_key)
        self._send_counter = 0
        self._recv_counter = 0

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)

    def seal(self, plaintext: bytes) -> bytes:
        """Encrypt one frame. The counter is the nonce and is never sent."""
        if len(plaintext) > MAX_FRAME_PLAINTEXT:
            raise ProtocolError("frame too large")
        frame = self._send.encrypt(self._nonce(self._send_counter), plaintext, PROTOCOL)
        self._send_counter += 1
        return frame

    def open(self, frame: bytes) -> bytes:
        """Decrypt the next expected frame, or raise `ProtocolError`.

        A frame that is replayed, dropped before it, reordered or modified does not decrypt
        under the expected counter, so the caller must close the connection on any error.
        """
        if len(frame) > MAX_FRAME_PLAINTEXT + 16:
            raise ProtocolError("frame too large")
        try:
            plaintext = self._recv.decrypt(self._nonce(self._recv_counter), frame, PROTOCOL)
        except Exception:
            raise ProtocolError("bad frame") from None
        self._recv_counter += 1
        return plaintext


class ClientHandshake:
    """Worker side of the handshake."""

    def __init__(self, hub_public_key: bytes, worker_id: str = "") -> None:
        """Prepare a hello. `hub_public_key` is the pinned 32-byte Ed25519 key of the hub."""
        encoded = worker_id.encode()
        if len(encoded) > MAX_WORKER_ID_LENGTH:
            raise ProtocolError("worker id too long")
        self._hub_key = Ed25519PublicKey.from_public_bytes(hub_public_key)
        self._eph = X25519PrivateKey.generate()
        self.hello = (
            b"AH1" + _raw(self._eph.public_key()) + os.urandom(32) + bytes([len(encoded)]) + encoded
        )
        self.transcript: bytes = b""

    def finish(self, server_hello: bytes) -> Channel:
        """Check the hub's answer and return the session channel.

        Raises `ProtocolError` if the signature does not match the pinned key.
        """
        if len(server_hello) != SERVER_HELLO_LENGTH:
            raise ProtocolError("bad server hello")
        hub_eph, hub_nonce, signature = (
            server_hello[:32],
            server_hello[32:64],
            server_hello[64:],
        )
        self.transcript = _transcript_hash(self.hello, hub_eph, hub_nonce)
        try:
            self._hub_key.verify(signature, b"armadok-hub-v1" + self.transcript)
        except InvalidSignature:
            raise ProtocolError("hub identity does not match the pinned key") from None
        shared = self._eph.exchange(X25519PublicKey.from_public_bytes(hub_eph))
        w2h, h2w = _session_keys(shared, self.transcript)
        return Channel(send_key=w2h, recv_key=h2w)


class ServerHandshake:
    """Hub side of the handshake."""

    def __init__(self, hub_private_key: Ed25519PrivateKey) -> None:
        """Create a handshake signed with the hub's long-term key."""
        self._key = hub_private_key
        self.worker_id = ""
        self.transcript = b""

    def respond(self, client_hello: bytes) -> tuple[bytes, Channel]:
        """Answer a hello; return `(server_hello, channel)`.

        The worker is NOT authenticated yet: the caller must read its encrypted auth message.
        """
        if len(client_hello) < HELLO_FIXED_LENGTH or client_hello[:3] != b"AH1":
            raise ProtocolError("bad hello")
        id_length = client_hello[3 + 32 + 32]
        if len(client_hello) != HELLO_FIXED_LENGTH + id_length:
            raise ProtocolError("bad hello length")
        try:
            self.worker_id = client_hello[HELLO_FIXED_LENGTH:].decode()
        except UnicodeDecodeError:
            raise ProtocolError("bad worker id") from None
        worker_eph = client_hello[3:35]
        eph = X25519PrivateKey.generate()
        hub_eph = _raw(eph.public_key())
        hub_nonce = os.urandom(32)
        self.transcript = _transcript_hash(client_hello, hub_eph, hub_nonce)
        signature = self._key.sign(b"armadok-hub-v1" + self.transcript)
        shared = eph.exchange(X25519PublicKey.from_public_bytes(worker_eph))
        w2h, h2w = _session_keys(shared, self.transcript)
        return hub_eph + hub_nonce + signature, Channel(send_key=h2w, recv_key=w2h)


# --- proofs made inside the encrypted channel ---------------------------------------------


def worker_signature(worker_key: Ed25519PrivateKey, purpose: bytes, transcript: bytes) -> bytes:
    """Sign this connection's transcript to prove the worker owns its identity key."""
    return worker_key.sign(b"armadok-worker-v1 " + purpose + transcript)


def verify_worker_signature(
    worker_public_key: bytes, signature: bytes, purpose: bytes, transcript: bytes
) -> bool:
    """Check a worker's signature; never raises."""
    try:
        Ed25519PublicKey.from_public_bytes(worker_public_key).verify(
            signature, b"armadok-worker-v1 " + purpose + transcript
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def enrolment_private_key(token_secret: str) -> Ed25519PrivateKey:
    """Derive an Ed25519 key from the token secret (worker side only).

    The hub stores just the public half, so a copy of its database cannot be used to enrol:
    it can check a proof but not make one.
    """
    seed = hashlib.sha256(b"armadok-enrol-v1" + token_secret.encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def enrolment_public_hex(token_secret: str) -> str:
    """Return the public half of the enrolment key, which is what the hub stores."""
    return public_key_bytes(enrolment_private_key(token_secret)).hex()


def enrolment_proof(token_secret: str, transcript: bytes) -> bytes:
    """Prove knowledge of the token for this connection only (signature over the transcript)."""
    return enrolment_private_key(token_secret).sign(b"armadok-enrol-proof " + transcript)


def verify_enrolment_proof(public_key_hex: str, proof: bytes, transcript: bytes) -> bool:
    """Check an enrolment proof against the stored public key; never raises."""
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            proof, b"armadok-enrol-proof " + transcript
        )
    except (InvalidSignature, ValueError):
        return False
    return True
