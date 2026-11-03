"""Application factory: wires the module registry, the database and the API routes."""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from armadok.core import crypto
from armadok.core.audit import AUDIT_MIGRATION
from armadok.core.config import Settings, load_settings
from armadok.core.db import apply_migrations, connect
from armadok.core.deps import Authenticator
from armadok.core.errors import ApiError
from armadok.core.grants import GRANTS_MIGRATION
from armadok.core.registry import Registry, load_registry
from armadok.modules.enabled import ENABLED_MODULES
from armadok.web import add_headers, mount_web


def create_app(
    settings: Settings | None = None,
    registry: Registry | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Fails at start-up if the module set is inconsistent (see `Registry`).
    `authenticator` turns a request into a `Principal`; until a login module provides one,
    protected routes answer 401. Arguments also let tests inject their own settings and modules.
    """
    settings = settings or load_settings()
    registry = registry or load_registry(ENABLED_MODULES)

    crypto.validate(settings)
    conn = connect(settings.db_path)
    apply_migrations(
        conn,
        [
            AUDIT_MIGRATION,
            GRANTS_MIGRATION,
            *(m for module in registry.modules for m in module.migrations),
        ],
    )
    for module in registry.modules:
        for hook in module.startup:
            hook(conn)
    conn.close()

    app = FastAPI(
        title="Armadok",
        version=os.environ.get("ARMADOK_VERSION", "0.0.0"),
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.registry = registry
    app.state.settings = settings
    app.state.authenticator = authenticator or registry.authenticator()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        """Add caching and security headers (see `web.add_headers`)."""
        response = await call_next(request)
        add_headers(request, response)
        return response

    @app.exception_handler(ApiError)
    def api_error(_: Request, exc: ApiError) -> JSONResponse:
        """Return errors as a stable code; the interface translates it."""
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status_code)

    @app.get("/api/v1/health", tags=["platform"])
    def health() -> dict[str, str]:
        """Liveness probe: answers `ok` when the process is up."""
        return {"status": "ok"}

    @app.get("/api/v1/modules", tags=["platform"])
    def modules() -> dict[str, list[str]]:
        """Names of the enabled modules, so the interface shows only what exists."""
        return {"modules": [m.name for m in registry.modules]}

    for module in registry.modules:
        for router in module.routes:
            app.include_router(router, prefix="/api/v1")

    # Last, so that every API route wins over the interface's catch-all.
    mount_web(app, settings.web_dir)

    return app
