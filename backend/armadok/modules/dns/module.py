"""Declaration of the `dns` module and its providers."""

from armadok.core.module import Module, Provider, Right, SearchProvider, Signal
from armadok.modules.dns import repository
from armadok.modules.dns import search as _search
from armadok.modules.dns.node_routes import router as node_router
from armadok.modules.dns.providers.duckdns import DuckDnsProvider
from armadok.modules.dns.providers.local import LocalProvider
from armadok.modules.dns.providers.ovh import OvhProvider
from armadok.modules.dns.routes import router
from armadok.modules.dns.service import on_node_deleted


def _provider(name, impl):
    return Provider(kind="dns", name=name, impl=impl, capabilities=impl.capabilities)


module = Module(
    name="dns",
    search=(SearchProvider("dns", _search.run),),
    requires=("nodes", "workers", "relations"),
    routes=(router, node_router),
    rights=(
        # Managing one's own provider accounts and domains: they only concern the account.
        Right("dns.use", baseline=True),
        # On a node: see (and, with write, change) the domains attached to it.
        Right("dns.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        Right("dns.write", depends_on=("dns.read",), node_owner=True, level="domains"),
    ),
    signals=(Signal("dns.changed", "dns.read"),),
    audit_events=(
        "dns.account_create",
        "dns.account_delete",
        "dns.domain_create",
        "dns.domain_attach",
        "dns.domain_delete",
        "dns.record_create",
        "dns.record_update",
        "dns.record_delete",
    ),
    migrations=(repository.MIGRATION, repository.RESTRICTIONS_MIGRATION),
    listeners=(("node.deleted", on_node_deleted),),
    # A new provider = one file under providers/ and one line here; nothing else changes.
    providers=(
        _provider("local", LocalProvider),
        _provider("duckdns", DuckDnsProvider),
        _provider("ovh", OvhProvider),
    ),
)
