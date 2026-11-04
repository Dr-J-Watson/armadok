"""Declaration of the `traefik` module."""

from armadok.core.module import Module, RelationProvider, Right, Signal, WorkerOp
from armadok.modules.traefik import relations
from armadok.modules.traefik.domain_routes import router as domain_router
from armadok.modules.traefik.middleware_routes import router as middleware_router
from armadok.modules.traefik.routes import router
from armadok.modules.traefik.sites_routes import router as sites_router
from armadok.modules.traefik.static_routes import router as static_router

_NODE_TYPES = ("network", "full")

module = Module(
    name="traefik",
    requires=("workers",),
    routes=(router, sites_router, middleware_router, domain_router, static_router),
    rights=(
        Right("traefik.read", depends_on=("nodes.read",), node_owner=True, level="view"),
        Right("traefik.files.read", depends_on=("traefik.read",), node_owner=True, level="edit"),
        Right(
            "traefik.files.write", depends_on=("traefik.files.read",), node_owner=True, level="edit"
        ),
        # Building sites from the form: routes real traffic to any address, so not for keys.
        Right(
            "traefik.sites.write",
            depends_on=("traefik.read",),
            node_owner=True,
            sensitive=True,
            level="edit",
        ),
        # The static file decides entry points, the API and certificate resolvers, and a mistake
        # stops the proxy: its own sensitive right, never for a key.
        Right(
            "traefik.static.write",
            depends_on=("traefik.files.read",),
            node_owner=True,
            sensitive=True,
            level="admin",
        ),
    ),
    signals=(Signal("traefik.changed", "traefik.read"),),
    audit_events=(
        "traefik.file_read",
        "traefik.file_write",
        "traefik.site_create",
        "traefik.site_update",
        "traefik.site_pause",
        "traefik.site_resume",
        "traefik.site_rename",
        "traefik.site_delete",
        "traefik.history_restore",
        "traefik.middleware_create",
        "traefik.middleware_update",
        "traefik.middleware_delete",
        "traefik.middleware_rename",
        "traefik.static_read",
        "traefik.static_write",
        "traefik.static_history_read",
        "traefik.domain_pause",
        "traefik.domain_resume",
    ),
    relations=(
        RelationProvider(
            name="traefik", right="traefik.read", node_types=_NODE_TYPES, provide=relations.provide
        ),
    ),
    worker_ops=(
        WorkerOp("traefik.files.list", _NODE_TYPES),
        WorkerOp("traefik.file.read", _NODE_TYPES),
        WorkerOp("traefik.file.write", _NODE_TYPES),
        WorkerOp("traefik.middleware.write", _NODE_TYPES),
        WorkerOp("traefik.file.delete", _NODE_TYPES),
        WorkerOp("traefik.snapshot", _NODE_TYPES),
        WorkerOp("traefik.sites.list", _NODE_TYPES),
        WorkerOp("traefik.site.write", _NODE_TYPES),
        WorkerOp("traefik.site.pause", _NODE_TYPES),
        WorkerOp("traefik.site.resume", _NODE_TYPES),
        WorkerOp("traefik.site.delete", _NODE_TYPES),
        WorkerOp("traefik.history.list", _NODE_TYPES),
        WorkerOp("traefik.history.read", _NODE_TYPES),
        WorkerOp("traefik.static.read", _NODE_TYPES),
        WorkerOp("traefik.static.write", _NODE_TYPES),
        WorkerOp("traefik.static.history.list", _NODE_TYPES),
        WorkerOp("traefik.static.history.read", _NODE_TYPES),
    ),
)
