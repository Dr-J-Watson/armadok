"""The worker's persistent identity: its signing key and, once enrolled, its id."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from armadok.worker_protocol.channel import (
    load_ed25519_private,
    private_key_bytes,
)

STATE_FILE = "worker_state.json"


@dataclass
class WorkerState:
    """Identity key plus the id the hub gave at enrolment (empty before)."""

    key: Ed25519PrivateKey
    worker_id: str = ""


def _path(directory: Path) -> Path:
    return directory / STATE_FILE


def load_state(directory: Path) -> WorkerState:
    """Load the state, creating a fresh identity key on first start."""
    path = _path(directory)
    if path.exists():
        data = json.loads(path.read_text())
        return WorkerState(
            load_ed25519_private(bytes.fromhex(data["key"])), data.get("worker_id", "")
        )
    state = WorkerState(Ed25519PrivateKey.generate())
    save_state(directory, state)
    return state


def save_state(directory: Path, state: WorkerState) -> None:
    """Write the state with owner-only permissions (it holds a private key)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"key": private_key_bytes(state.key).hex(), "worker_id": state.worker_id})
    tmp = _path(directory).with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    os.replace(tmp, _path(directory))
