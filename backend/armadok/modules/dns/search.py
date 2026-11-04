"""Search provider of the dns module: the domains the caller can see."""

from __future__ import annotations

from armadok.core.module import SearchContext, SearchHit
from armadok.modules.dns import repository, service

MAX_SCANNED = 1000  # domains looked at per search, whatever the number of matches


def run(ctx: SearchContext, text: str, limit: int) -> list[SearchHit]:
    """Return domains whose name contains `text`, among those the caller may use."""
    if not ctx.who.has("dns.use", ctx.registry.rights()):
        return []
    hits = []
    for row in repository.list_domains(ctx.conn)[:MAX_SCANNED]:
        if text in row["name"].lower() and service.access(ctx.registry, ctx.conn, ctx.who, row):
            hits.append(SearchHit("domain", row["name"], "/dns", ""))
            if len(hits) >= limit:
                break
    return hits
