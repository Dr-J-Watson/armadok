"""The server's own key pair for browser push (VAPID), made on first use and kept encrypted.

The private key never leaves the hub; the public half is given to browsers so that a push
service only accepts messages signed by this Armadok.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from armadok.core import crypto, webpush
from armadok.core.db import Migration

MIGRATION = Migration(
    "notifications-0006-push-key",
    """
    CREATE TABLE webpush_key (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        private TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
)


def load(settings: Any) -> ec.EllipticCurvePrivateKey:
    """Return the VAPID key, making and storing it the first time (safe if two ask at once)."""
    conn: sqlite3.Connection = settings.open_db()
    try:
        row = conn.execute("SELECT private FROM webpush_key WHERE id = 1").fetchone()
        if row is None:
            sealed = crypto.encrypt(settings, webpush.vapid_private_pem(webpush.new_vapid_key()))
            conn.execute(
                "INSERT OR IGNORE INTO webpush_key (id, private, created_at) VALUES (1, ?, ?)",
                (sealed, datetime.now(UTC).isoformat()),
            )
            conn.commit()
            row = conn.execute("SELECT private FROM webpush_key WHERE id = 1").fetchone()
        return webpush.load_vapid_key(crypto.decrypt(settings, row["private"]))
    finally:
        conn.close()
