"""Declaration of the `traffic` module: volumes, errors and response times of the sites."""

from armadok.core.module import Module, Right, WorkerOp
from armadok.modules.traffic import accesslog, accesslog_repository, repository
from armadok.modules.traffic.routes import router
from armadok.modules.traffic.service import watch_loop

_NODE_TYPES = ("network", "full")

module = Module(
    name="traffic",
    requires=("workers",),
    routes=(router,),
    rights=(
        # Aggregate figures only (no visitor address in Traefik's metrics).
        Right("traffic.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        # The access log holds visitor addresses: reading it, seeing the addresses whole, and
        # erasing it are three separate rights, none of them for API keys.
        Right(
            "traffic.logs",
            depends_on=("traffic.read",),
            node_owner=True,
            sensitive=True,
            level="operate",
        ),
        Right(
            "traffic.read_ip",
            depends_on=("traffic.logs",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
        Right(
            "traffic.purge",
            depends_on=("traffic.logs",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    audit_events=("traffic.logs_read", "traffic.ip_read", "traffic.purge"),
    worker_ops=(
        WorkerOp("traffic.scrape", _NODE_TYPES),
        WorkerOp("traffic.status", _NODE_TYPES),
        WorkerOp("traffic.accesslog", _NODE_TYPES),
    ),
    migrations=(repository.MIGRATION, accesslog_repository.MIGRATION),
    tasks=(watch_loop, accesslog.watch_loop),
)
