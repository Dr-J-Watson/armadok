"""Serves the built web interface from the hub, with security headers.

The interface is a single-page app: any path that is not a real file returns `index.html`
and the browser-side router takes over. API paths never fall through to it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# No inline script or style is used by the interface, so none is allowed: an injected script
# cannot run. Network calls (API and worker WebSocket) are limited to the same origin.
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
IMMUTABLE = "public, max-age=31536000, immutable"  # built asset names carry a content hash


def mount_web(app: FastAPI, directory: Path | None) -> bool:
    """Serve `directory` as the interface; return False if there is nothing to serve."""
    if directory is None or not (directory / "index.html").is_file():
        return False
    root = directory.resolve()
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def interface(path: str) -> Response:
        """Return a real file of the interface, else `index.html` (client-side routing)."""
        if path.startswith("api/") or path == "api":
            return JSONResponse({"error": {"code": "common.not_found"}}, status_code=404)
        candidate = (root / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")

    return True


def add_headers(request: Request, response: Response) -> None:
    """Set caching and security headers according to what is being served."""
    path = request.url.path
    if path.startswith("/api/") or path.rstrip("/") in getattr(request.app.state, "root_paths", ()):
        # Responses can carry tokens, secrets and personal data: never cached.
        response.headers["Cache-Control"] = "no-store"
        return
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = IMMUTABLE if path.startswith("/assets/") else "no-cache"
