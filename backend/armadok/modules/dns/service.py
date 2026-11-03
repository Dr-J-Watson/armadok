"""Opening a provider for a domain, and who may do what with a domain."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from armadok.core import crypto
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.dns import repository
from armadok.modules.dns.base import DnsProvider, ProviderError
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.users import repository as users_repo

LOCAL = "local"


def provider_class(registry: Registry, name: str):
    """Return the registered provider class for `name`."""
    entry = registry.providers("dns").get(name)
    if entry is None:
        raise ApiError("dns.unknown_provider", 422)
    return entry


def open_provider(app: Any, registry: Registry, conn: sqlite3.Connection, domain: sqlite3.Row):
    """Build the provider of a domain from its account's (decrypted) credentials.

    The account must belong to the domain's owner: a domain can never run on someone else's keys.
    """
    if domain["account_id"] is None:
        entry = provider_class(registry, LOCAL)
        return entry.impl(entry.impl.credentials())
    account = repository.get_account(conn, domain["account_id"])
    if account is None or account["owner_id"] != domain["owner_id"]:
        raise ApiError("dns.account_missing", 409)
    entry = provider_class(registry, account["provider"])
    data = json.loads(crypto.decrypt(app.state.settings, account["credentials"]))
    return entry.impl(entry.impl.credentials.model_validate(data))


def capabilities(registry: Registry, conn: sqlite3.Connection, domain: sqlite3.Row) -> list[str]:
    """Return what the domain's provider can do."""
    if domain["account_id"] is None:
        return sorted(provider_class(registry, LOCAL).capabilities)
    account = repository.get_account(conn, domain["account_id"])
    return sorted(provider_class(registry, account["provider"]).capabilities) if account else []


def verified(registry: Registry, conn: sqlite3.Connection, domain: sqlite3.Row) -> bool:
    """Return True if the zone is proven to belong to the owner's provider account.

    An account-less (local) domain is only a name anyone could have typed, so it never
    outranks a verified one when a host is matched to a domain.
    """
    return "zones" in capabilities(registry, conn, domain)


def access(
    registry: Registry, conn: sqlite3.Connection, who: Principal, domain: sqlite3.Row
) -> str | None:
    """Return the caller's access to a domain: "owner", "write", "read" or None.

    `dns.use` is needed for everything, so an API key only touches DNS if it was given that
    right explicitly. The owner then has full access. Members of the node the domain is attached
    to get what their node rights say (`dns.read` / `dns.write`), and only while that node still
    exists and the owner still sees it; they never see the account's keys.
    """
    if not who.has("dns.use", registry.rights()):
        return None
    if domain["owner_id"] == who.id:
        return "owner"
    node_id = domain["node_id"]
    if node_id is None or nodes_repo.get_node(conn, node_id) is None:
        return None
    owner = users_repo.get_user(conn, domain["owner_id"])
    owner_principal = registry.principal_for(conn, domain["owner_id"])
    if (
        owner is None
        or not owner["is_active"]
        or not owner_principal.has("nodes.read", registry.rights(), f"node:{node_id}")
    ):
        return None  # the person who lent the keys no longer has the node: the lending stops
    scope = f"node:{node_id}"
    if who.has("dns.write", registry.rights(), scope):
        return "write"
    if who.has("dns.read", registry.rights(), scope):
        return "read"
    return None


def member_may(domain: sqlite3.Row, record_type: str, name: str) -> bool:
    """Return True if a member (not the owner) may write this record on the owner's account.

    By default members may only manage plain address/alias records below the apex, never
    mail, name servers, certificate-authority or `_service`/`_acme-challenge` style labels.
    """
    if record_type not in domain["member_types"].split(","):
        return False
    if name == "" and not domain["member_apex"]:
        return False
    if name.startswith("_") or "._" in name:
        return False
    allowed = [n for n in domain["member_names"].split(",") if n]
    return not allowed or any(name == n or name.endswith("." + n) for n in allowed)


def provider_error(error: ProviderError) -> ApiError:
    """Turn a provider's failure into an API error (a bad gateway: the provider is at fault)."""
    return ApiError(error.code, 502)


__all__ = ["DnsProvider", "access", "capabilities", "member_may", "open_provider", "provider_error"]


async def on_node_deleted(app: Any, node_id: str) -> None:
    """Detach the domains of a deleted node: its members must stop using the owners' accounts."""
    conn = app.state.settings.open_db()
    try:
        repository.detach_node(conn, node_id)
    finally:
        conn.close()
