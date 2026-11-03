"""The hub's long-term identity key, pinned by every worker.

Stored as a file in the data folder (mode 0600), created on first use. Workers only ever
see the public half; losing the private half means re-enrolling every worker.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from armadok.core.config import Settings
from armadok.worker_protocol.channel import (
    load_ed25519_private,
    private_key_bytes,
    public_key_bytes,
)

KEY_FILE = "hub_identity.key"


_cache: dict[Path, Ed25519PrivateKey] = {}


def hub_private_key(settings: Settings) -> Ed25519PrivateKey:
    """Load the hub key (cached in memory), creating it with owner-only permissions if needed.

    Creation is atomic: the key is written to a private temporary file, then hard-linked into
    place. A concurrent process never sees a half-written file, and an existing key is never
    overwritten. A key file readable by others is refused rather than silently used.
    """
    path: Path = settings.data_dir / KEY_FILE
    if path in _cache:
        return _cache[path]
    if not path.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{KEY_FILE}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(private_key_bytes(Ed25519PrivateKey.generate()))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass  # another process won the race; use its key
        finally:
            os.unlink(tmp)
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"{path} must not be readable by other users (chmod 600)")
    key = load_ed25519_private(path.read_bytes())
    _cache[path] = key
    return key


def hub_public_key_b64(settings: Settings) -> str:
    """Return the public key in the form operators paste into the worker configuration."""
    return base64.urlsafe_b64encode(public_key_bytes(hub_private_key(settings))).decode()
