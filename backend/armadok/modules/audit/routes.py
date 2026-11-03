"""Audit endpoints: browse (with filters and paging), export, and check the chain.

Visibility follows rights: `audit.read` everywhere shows every entry; `audit.read` on a node
shows only the entries about that node. Entries carry names and counts, never contents (the
modules that write them see to that).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import ALL_SCOPES, Principal
from armadok.core.time_utils import still_valid

router = APIRouter(prefix="/audit", tags=["audit"])

MAX_PAGE = 200
MAX_EXPORT = 10_000
_COLUMNS = "id, at, actor, event, object_type, object_id, outcome, details"


MAX_SCOPED_NODES = 500  # keeps the SQL parameter count far below SQLite's limit


def _visibility(who: Principal, registry: Registry) -> tuple[bool, list[str]]:
    """Return `(sees_everything, node_ids)` for the viewer, or refuse if they see nothing."""
    if who.has("audit.read", registry.rights(), ALL_SCOPES):
        return True, []
    grants = [g for g in who.grants if g.right == "audit.read" and still_valid(g.expires_at)]
    nodes = [g.scope[5:] for g in grants if g.scope.startswith("node:")][:MAX_SCOPED_NODES]
    if not nodes:
        raise MissingRight()
    return False, nodes


def _query(
    conn: sqlite3.Connection,
    who: Principal,
    registry: Registry,
    *,
    actor: str | None,
    event: str | None,
    outcome: str | None,
    node_id: str | None,
    before_id: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return matching entries, newest first, restricted to what the viewer may see."""
    everything, nodes = _visibility(who, registry)
    where, args = [], []
    if not everything:
        where.append(f"(object_type = 'node' AND object_id IN ({','.join('?' * len(nodes))}))")
        args += nodes
    if node_id:
        where.append("(object_type = 'node' AND object_id = ?)")
        args.append(node_id)
    if actor:
        # Match the account's name (what the interface shows) or the raw actor text.
        pattern = "%" + actor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM users WHERE username LIKE ? ESCAPE '\\'", (pattern,)
            )
        ]
        marks = ",".join("?" * len(ids))
        clause_parts = ["actor LIKE ? ESCAPE '\\'"]
        if ids:
            clause_parts.append(f"substr(actor, 1, 32) IN ({marks})")
        where.append("(" + " OR ".join(clause_parts) + ")")
        args += [pattern, *ids]
    if event:
        where.append("event = ?")
        args.append(event)
    if outcome:
        where.append("outcome = ?")
        args.append(outcome)
    if before_id:
        where.append("id < ?")
        args.append(before_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM audit_log {clause} ORDER BY id DESC LIMIT ?", (*args, limit)
    ).fetchall()
    # Only the accounts that appear in these rows are looked up.
    wanted = list({row["actor"].partition(" via ")[0] for row in rows})
    names = {}
    if wanted:
        marks = ",".join("?" * len(wanted))
        names = {
            r["id"]: r["username"]
            for r in conn.execute(f"SELECT id, username FROM users WHERE id IN ({marks})", wanted)
        }
    entries = []
    for row in rows:
        entry = dict(row)
        entry["details"] = json.loads(entry["details"] or "{}")
        # "user-id via key:abc" -> the account's name, keeping the key mention.
        account, _, via = entry["actor"].partition(" via ")
        entry["actor_name"] = names.get(account, account) + (f" via {via}" if via else "")
        entries.append(entry)
    return entries


@router.get("", summary="Browse the audit log")
def browse(
    actor: str | None = Query(default=None, max_length=100),
    event: str | None = Query(default=None, max_length=60),
    outcome: str | None = Query(default=None, pattern="^(success|denied|failure)$"),
    node_id: str | None = Query(default=None, max_length=64),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
    registry: Registry = Depends(get_registry),
) -> dict:
    """Return a page of entries, newest first. Pass the last `id` as `before_id` for the next."""
    entries = _query(
        conn,
        who,
        registry,
        actor=actor,
        event=event,
        outcome=outcome,
        node_id=node_id,
        before_id=before_id,
        limit=limit,
    )
    return {
        "entries": entries,
        "next_before_id": entries[-1]["id"] if len(entries) == limit else None,
    }


@router.get("/export", summary="Export the audit log")
def export(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    actor: str | None = Query(default=None, max_length=100),
    event: str | None = Query(default=None, max_length=60),
    outcome: str | None = Query(default=None, pattern="^(success|denied|failure)$"),
    node_id: str | None = Query(default=None, max_length=64),
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
    registry: Registry = Depends(get_registry),
) -> Response:
    """Return up to 10,000 matching entries as JSON or CSV, with the same visibility rules.

    Exports are themselves audited (what was exported, not the content).
    """
    entries = _query(
        conn,
        who,
        registry,
        actor=actor,
        event=event,
        outcome=outcome,
        node_id=node_id,
        before_id=None,
        limit=MAX_EXPORT,
    )
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "audit.export",
        object_type="audit",
        details={"format": format, "count": len(entries), "node": node_id},
    )
    # Downloaded as a file, and never sniffed into something executable by the browser.
    headers = {
        "Content-Disposition": f"attachment; filename=audit.{format}",
        "X-Content-Type-Options": "nosniff",
    }
    if format == "json":
        return Response(json.dumps(entries), media_type="application/json", headers=headers)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["id", "at", "actor", "event", "object_type", "object_id", "outcome", "details"]
    )
    for e in entries:
        writer.writerow(
            [
                _safe_cell(v)
                for v in (
                    e["id"],
                    e["at"],
                    e["actor_name"],
                    e["event"],
                    e["object_type"],
                    e["object_id"],
                    e["outcome"],
                    json.dumps(e["details"]),
                )
            ]
        )
    return Response(out.getvalue(), media_type="text/csv", headers=headers)


def _safe_cell(value: Any) -> str:
    """Neutralise spreadsheet formulas: a cell starting with = + - @ (or tab, CR) would be run.

    Leading whitespace is looked past too, since some spreadsheets ignore it before a formula.
    """
    text = "" if value is None else str(value)
    dangerous = ("=", "+", "-", "@", "\t", "\r", "\n")
    return "'" + text if text[:1] in dangerous or text.lstrip()[:1] in dangerous[:4] else text


@router.get("/verify", summary="Check that the audit log was not altered")
def verify(
    conn: sqlite3.Connection = Depends(get_db),
    who: Principal = Depends(get_principal),
    registry: Registry = Depends(get_registry),
) -> dict:
    """Check the whole chain. Needs `audit.read` everywhere: it covers every entry.

    `signed: false` means no signing key is configured, so the result proves nothing against
    someone who can write to the database file.
    """
    who.require("audit.read", registry.rights(), ALL_SCOPES)
    return audit.verify_chain(conn)
