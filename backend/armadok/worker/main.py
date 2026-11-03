"""Entry point: keep a connection to the hub, reconnecting with a growing delay.

Configuration (environment): `ARMADOK_HUB_URL` (ws:// or wss:// address of
`/api/v1/worker/connect`), `ARMADOK_HUB_KEY` (the hub public key shown at enrolment),
`ARMADOK_ENROL_TOKEN` (only for the first start) and `ARMADOK_WORKER_DIR` (state folder).
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from pathlib import Path

from websockets.sync.client import connect as ws_connect

from armadok.worker import client
from armadok.worker.ops import OPERATIONS
from armadok.worker.state import load_state, save_state
from armadok.worker_protocol.channel import MAX_FRAME_PLAINTEXT, ProtocolError

log = logging.getLogger("armadok.worker")


class _WebSocketTransport:
    """Adapts a `websockets` sync connection to the worker's `Transport`."""

    def __init__(self, ws) -> None:
        self._ws = ws

    def send(self, data: bytes) -> None:
        self._ws.send(data)

    def recv(self) -> bytes:
        data = self._ws.recv()
        if not isinstance(data, bytes):
            raise ProtocolError("text frame received")
        return data


def _close_on_stop(ws, stop: threading.Event, done: threading.Event) -> None:
    """Close the connection when asked to stop, so a blocked read returns promptly."""
    while not done.is_set():
        if stop.wait(0.2):
            ws.close()
            return


def run(
    url: str,
    hub_key_b64: str,
    state_dir: Path,
    enrol_token: str | None,
    stop: threading.Event | None = None,
) -> None:
    """Connect, serve, and reconnect until `stop` is set."""
    stop = stop or threading.Event()
    hub_key = base64.urlsafe_b64decode(hub_key_b64)
    state = load_state(state_dir)
    delay = 1.0
    while not stop.is_set():
        try:
            with ws_connect(url, max_size=MAX_FRAME_PLAINTEXT + 64) as ws:
                done = threading.Event()
                threading.Thread(target=_close_on_stop, args=(ws, stop, done), daemon=True).start()
                transport = _WebSocketTransport(ws)
                channel = client.connect(transport, hub_key, state, enrol_token)
                save_state(state_dir, state)
                delay = 1.0
                log.info("connected to the hub")
                try:
                    client.serve(transport, channel, OPERATIONS, stop.is_set)
                finally:
                    done.set()
        except (OSError, ProtocolError, client.EnrolmentError, ConnectionError) as error:
            log.warning("connection lost: %s", error)
        except Exception as error:  # websockets raises its own exception family
            log.warning("connection ended: %s", type(error).__name__)
        stop.wait(delay)
        delay = min(delay * 2, 60.0)


def main() -> None:
    """Read the configuration from the environment and run forever."""
    logging.basicConfig(level=logging.INFO)
    run(
        os.environ["ARMADOK_HUB_URL"],
        os.environ["ARMADOK_HUB_KEY"],
        Path(os.environ.get("ARMADOK_WORKER_DIR", "worker-data")),
        os.environ.get("ARMADOK_ENROL_TOKEN") or None,
    )


if __name__ == "__main__":
    main()
