"""Storage of image scan results: the latest one per image and machine."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "imagescan-0001-results",
    """
    CREATE TABLE image_scans (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        image_id TEXT NOT NULL,
        image_name TEXT NOT NULL,
        os TEXT NOT NULL,
        os_version TEXT NOT NULL,
        packages INTEGER NOT NULL,
        scanned_at TEXT NOT NULL,
        status TEXT NOT NULL,
        findings TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (node_id, image_id)
    );
    """,
)

MAX_KEPT = 300  # findings kept per image (the worst first); the count says how many there were
RANK = {"critical": 0, "high": 1, "unknown": 2, "moderate": 3, "low": 4}


def get(conn: sqlite3.Connection, node_id: str, image_id: str) -> sqlite3.Row | None:
    """Return the latest scan of one image on one machine, or None."""
    return conn.execute(
        "SELECT * FROM image_scans WHERE node_id = ? AND image_id = ?", (node_id, image_id)
    ).fetchone()


def store(
    conn: sqlite3.Connection,
    node_id: str,
    image_id: str,
    name: str,
    *,
    os_id: str,
    os_version: str,
    packages: int,
    status: str,
    findings: list[dict[str, Any]],
) -> None:
    """Replace the latest result for an image; keep the worst findings."""
    kept = sorted(findings, key=lambda f: (RANK.get(f["severity"], 5), f["component"], f["id"]))
    conn.execute(
        "INSERT INTO image_scans (node_id, image_id, image_name, os, os_version, packages,"
        " scanned_at, status, findings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(node_id, image_id) DO UPDATE SET image_name = excluded.image_name,"
        " os = excluded.os, os_version = excluded.os_version, packages = excluded.packages,"
        " scanned_at = excluded.scanned_at, status = excluded.status,"
        " findings = excluded.findings",
        (
            node_id,
            image_id,
            name[:200],
            os_id,
            os_version,
            packages,
            datetime.now(UTC).isoformat(),
            status,
            json.dumps(kept[:MAX_KEPT]),
        ),
    )
    conn.commit()


def view(row: sqlite3.Row) -> dict[str, Any]:
    """Return a scan as the interface shows it: counts by severity, then the findings."""
    findings = json.loads(row["findings"])
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "image_id": row["image_id"],
        "name": row["image_name"],
        "os": row["os"],
        "os_version": row["os_version"],
        "packages": row["packages"],
        "scanned_at": row["scanned_at"],
        "status": row["status"],
        "counts": counts,
        "findings": findings,
    }


def list_for_node(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """Return every stored scan of a machine, worst first."""
    rows = conn.execute("SELECT * FROM image_scans WHERE node_id = ?", (node_id,)).fetchall()
    shown = [view(r) for r in rows]
    shown.sort(
        key=lambda s: (
            min((RANK.get(k, 5) for k in s["counts"]), default=9),
            s["name"],
        )
    )
    return shown


MAX_IMAGES_PER_NODE = 200


def forget_others(conn: sqlite3.Connection, node_id: str, present: set[str]) -> None:
    """Drop results of images the machine no longer has, and keep at most 200 per machine."""
    for row in conn.execute(
        "SELECT image_id FROM image_scans WHERE node_id = ?", (node_id,)
    ).fetchall():
        if row["image_id"] not in present:
            conn.execute(
                "DELETE FROM image_scans WHERE node_id = ? AND image_id = ?",
                (node_id, row["image_id"]),
            )
    conn.execute(
        "DELETE FROM image_scans WHERE node_id = ? AND image_id NOT IN ("
        " SELECT image_id FROM image_scans WHERE node_id = ? ORDER BY scanned_at DESC LIMIT ?)",
        (node_id, node_id, MAX_IMAGES_PER_NODE),
    )
    conn.commit()
