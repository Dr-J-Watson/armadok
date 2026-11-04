"""Search provider of the users module: people, for those allowed to list them."""

from __future__ import annotations

from armadok.core.module import SearchContext, SearchHit
from armadok.modules.users import repository


def run(ctx: SearchContext, text: str, limit: int) -> list[SearchHit]:
    """Return accounts whose user name contains `text` (needs `users.read`)."""
    if not ctx.who.has("users.read", ctx.registry.rights()):
        return []
    hits = []
    for user in repository.list_users(ctx.conn):
        if text in user["username"].lower():
            hits.append(SearchHit("user", user["username"], "/users", user["kind"]))
            if len(hits) >= limit:
                break
    return hits
