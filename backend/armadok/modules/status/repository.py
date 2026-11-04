"""Storage of public status pages (one per probe at most)."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

from armadok.core.db import Migration

MIGRATION = Migration(
    "status-0001-pages",
    """
    CREATE TABLE status_pages (
        slug TEXT PRIMARY KEY,
        probe_id TEXT NOT NULL UNIQUE REFERENCES probes(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
)


def for_probe(conn: sqlite3.Connection, probe_id: str) -> sqlite3.Row | None:
    """Return the page of a probe, or None."""
    return conn.execute("SELECT * FROM status_pages WHERE probe_id = ?", (probe_id,)).fetchone()


def by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """Return a page by its address, or None."""
    return conn.execute("SELECT * FROM status_pages WHERE slug = ?", (slug,)).fetchone()


def publish(conn: sqlite3.Connection, probe_id: str, title: str, who: str) -> str:
    """Create the page of a probe (or change its title) and return its address.

    The address is random and long: it is the only thing that protects a page that anyone with
    the link may read, so it is never derived from a name.
    """
    existing = for_probe(conn, probe_id)
    if existing is not None:
        conn.execute("UPDATE status_pages SET title = ? WHERE slug = ?", (title, existing["slug"]))
        conn.commit()
        return existing["slug"]
    slug = secrets.token_urlsafe(18)
    conn.execute(
        "INSERT INTO status_pages (slug, probe_id, title, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (slug, probe_id, title, who, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return slug


def unpublish(conn: sqlite3.Connection, probe_id: str) -> bool:
    """Close the page of a probe; True if there was one. Its old address stops working."""
    cur = conn.execute("DELETE FROM status_pages WHERE probe_id = ?", (probe_id,))
    conn.commit()
    return cur.rowcount == 1


def public_view(conn: sqlite3.Connection, slug: str, bars: int) -> dict[str, Any] | None:
    """Return what the public may see of a page: a title, a state and a few dots. Nothing else.

    No host, no machine, no reason text (a reason can name an internal address).
    """
    page = by_slug(conn, slug)
    if page is None:
        return None
    probe = conn.execute(
        "SELECT id, enabled, state, last_checked, last_change FROM probes WHERE id = ?",
        (page["probe_id"],),
    ).fetchone()
    if probe is None:
        return None
    recent = conn.execute(
        "SELECT ok FROM probe_results WHERE probe_id = ? ORDER BY at DESC LIMIT ?",
        (probe["id"], bars),
    ).fetchall()
    stats = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(ok), 0) AS good FROM probe_results WHERE probe_id = ?",
        (probe["id"],),
    ).fetchone()
    # A paused probe says nothing about the site.
    state = probe["state"] if probe["enabled"] and probe["state"] in ("up", "down") else "unknown"
    return {
        "title": page["title"],
        "state": state,
        "since": probe["last_change"] if state != "unknown" else None,
        "checked_at": probe["last_checked"],
        "availability": round(100 * stats["good"] / stats["n"], 2) if stats["n"] else None,
        "recent": [bool(r["ok"]) for r in reversed(recent)],
    }
