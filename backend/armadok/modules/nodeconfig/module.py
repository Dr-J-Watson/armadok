"""Declaration of the `nodeconfig` module: a node's own settings, over the environment's roots."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.nodeconfig import catalogue, repository
from armadok.modules.nodeconfig.routes import router
from armadok.worker.nodeconfig import protect

_ALL = ("docker", "network", "full")


def _worker_values(conn, node_id: str) -> dict[str, str]:
    """Return the places a node narrowed: they travel with every call to its worker.

    Only the places its current type has: a node whose type changed keeps no hidden narrowing.
    """
    row = conn.execute("SELECT type FROM nodes WHERE id = ?", (node_id,)).fetchone()
    relevant = catalogue.PLACES_BY_TYPE.get(row["type"] if row else "", ())
    return {k: v for k, v in repository.all_for(conn, node_id).items() if k in relevant}


def _protect_data(conn, settings, registry) -> None:
    """Start-up: the hub's own data folder can never be reached by a narrowing (local node)."""
    protect(settings.data_dir)


module = Module(
    name="nodeconfig",
    requires=("workers", "users"),
    routes=(router,),
    rights=(
        Right("nodeconfig.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        # Decides what the worker may touch (inside its environment's roots): sensitive, so never
        # for API keys.
        Right(
            "nodeconfig.write",
            depends_on=("nodeconfig.read", "nodes.update"),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    audit_events=("nodeconfig.update",),
    worker_ops=(WorkerOp("config.describe", _ALL), WorkerOp("config.check", _ALL)),
    worker_context=(_worker_values,),
    migrations=(repository.MIGRATION,),
    startup=(_protect_data,),
)
