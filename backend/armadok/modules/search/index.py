"""A background index of what lives on each node, so the search can find it.

Looking for a container, a project, a site or a domain would mean asking every node at every
keystroke. Instead a background round asks each node's relation providers (the same source as
the map) every few minutes and keeps the names, per node and per provider. The search then
reads that table only, and shows a row only to someone who may read that provider's data on that
node, exactly as the map does. The index holds names, never content, and a node that
cannot be reached keeps its last known names until it answers again.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from armadok.core.db import Migration
from armadok.core.module import SearchContext, SearchHit
from armadok.core.rights import Grant, Principal

log = logging.getLogger("armadok.search.index")

MIGRATION = Migration(
    "search-0001-index",
    """
    CREATE TABLE search_index (
        node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        label TEXT NOT NULL,
        PRIMARY KEY (node_id, provider, type, object_id)
    );
    """,
)

ROUND_EVERY_SECONDS = 300
CHANGING_SIGNALS = frozenset(
    {"docker.changed", "projects.changed", "traefik.changed", "dns.changed", "nodes.changed"}
)
STARTUP_DELAY_SECONDS = 90
PROVIDER_TIMEOUT_SECONDS = 25
MAX_PER_PROVIDER = 3000  # names kept per node and provider
MAX_SCANNED = 600  # rows looked at for one search
# Types that are noise in a search box: a probe's state, a bare image digest...
SKIP_TYPES = frozenset({"probe", "image"})


def _indexer(registry: Any, node_id: str) -> Principal:
    """Build the indexer's own principal: it may read every provider's data, on one node.

    It lives only in this process and is never given to a request.
    """
    scope = f"node:{node_id}"
    return Principal(
        id="system:search-index",
        kind="background",
        grants=tuple(Grant(right, scope) for right in registry.rights()),
    )


def _replace(conn: sqlite3.Connection, node_id: str, provider: str, rows: list[tuple]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM search_index WHERE node_id = ? AND provider = ?", (node_id, provider)
        )
        conn.executemany(
            "INSERT OR REPLACE INTO search_index (node_id, provider, type, object_id, label)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


async def refresh_node(app: Any, node: dict[str, Any]) -> int:
    """Re-read one node's providers into the index; return how many names were kept."""
    registry = app.state.registry
    who, kept = _indexer(registry, node["id"]), 0
    for provider in registry.relation_providers():
        if node["type"] not in provider.node_types:
            continue
        try:
            data = await asyncio.wait_for(
                provider.provide(app, node["id"], who), PROVIDER_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001  (an unreachable node keeps its last names)
            continue
        rows = [
            (node["id"], provider.name, o.type, o.id[:500], o.label[:200])
            for o in data.objects
            if o.type not in SKIP_TYPES
        ][:MAX_PER_PROVIDER]

        def store(rows=rows, provider=provider.name) -> None:
            conn = app.state.settings.open_db()
            try:
                _replace(conn, node["id"], provider, rows)
            finally:
                conn.close()

        await asyncio.to_thread(store)
        kept += len(rows)
    return kept


async def refresh_all(app: Any) -> int:
    """One round over every node; return the number of names indexed."""
    from armadok.modules.nodes import repository as nodes_repo

    def machines() -> list[dict[str, Any]]:
        conn = app.state.settings.open_db()
        try:
            return [n for n in nodes_repo.list_nodes(conn)]
        finally:
            conn.close()

    total = 0
    for node in await asyncio.to_thread(machines):
        total += await refresh_node(app, node)
    return total


# Changes made through Armadok refresh the node's names a few seconds later (one refresh for a
# burst of changes); what happens outside Armadok is caught by the periodic round.
REFRESH_AFTER_CHANGE_SECONDS = 3
_pending: dict[tuple[int, str], asyncio.Task[None]] = {}


async def on_signal(
    app: Any, signal: str, node_id: str | None, actor_id: str | None = None
) -> None:
    """Re-index a node soon after one of its objects changed (listener of change signals)."""
    if node_id is None or signal not in CHANGING_SIGNALS or (id(app), node_id) in _pending:
        return

    async def later() -> None:
        try:
            await asyncio.sleep(REFRESH_AFTER_CHANGE_SECONDS)
            _pending.pop((id(app), node_id), None)

            def get() -> dict[str, Any] | None:
                from armadok.modules.nodes import repository as nodes_repo

                conn = app.state.settings.open_db()
                try:
                    node = nodes_repo.get_node(conn, node_id)
                    return dict(node) if node else None
                finally:
                    conn.close()

            node = await asyncio.to_thread(get)
            if node is not None:
                await refresh_node(app, node)
        except Exception:  # noqa: BLE001  (the periodic round will catch up)
            log.exception("search index refresh after a change failed")
        finally:
            _pending.pop((id(app), node_id), None)

    _pending[(id(app), node_id)] = asyncio.create_task(later())


async def index_loop(app: Any) -> None:
    """Refresh the index every few minutes until the application stops."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await refresh_all(app)
        except Exception:  # noqa: BLE001  (keep the loop alive)
            log.exception("search index round failed")
        await asyncio.sleep(ROUND_EVERY_SECONDS)


def run(ctx: SearchContext, text: str, limit: int) -> list[SearchHit]:
    """Return indexed objects whose name contains `text` and that the caller may read."""
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = ctx.conn.execute(
        "SELECT i.node_id, i.provider, i.type, i.object_id, i.label, n.name AS node_name"
        " FROM search_index i JOIN nodes n ON n.id = i.node_id"
        " WHERE n.deleted_at IS NULL AND lower(i.label) LIKE ? ESCAPE '\\'"
        " ORDER BY i.label LIMIT ?",
        (pattern, MAX_SCANNED),
    )
    rights = {p.name: p.right for p in ctx.registry.relation_providers()}
    catalogue, allowed, hits = ctx.registry.rights(), {}, []
    for row in rows:
        key = (row["node_id"], row["provider"])
        if key not in allowed:
            right = rights.get(row["provider"])
            allowed[key] = right is not None and ctx.who.has(
                right, catalogue, f"node:{row['node_id']}"
            )
        if not allowed[key]:
            continue
        hits.append(
            SearchHit(
                row["type"],
                row["label"],
                f"/nodes/{row['node_id']}?tab=map&focus={row['type']}:{row['object_id']}",
                row["node_name"],
            )
        )
        if len(hits) >= limit:
            break
    return hits
