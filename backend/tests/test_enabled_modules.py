"""The real application must load every module that exists (tests build their own lists)."""

from pathlib import Path

import armadok.modules as modules_package
from armadok.core.registry import load_registry
from armadok.modules.enabled import ENABLED_MODULES


def test_every_module_folder_is_enabled_and_the_registry_accepts_them():
    root = Path(modules_package.__file__).parent
    folders = {p.name for p in root.iterdir() if (p / "module.py").exists()}
    assert folders == set(ENABLED_MODULES)
    load_registry(ENABLED_MODULES)


def test_the_real_application_starts_with_all_modules(tmp_path):
    from fastapi.testclient import TestClient

    from armadok.core.config import Settings
    from armadok.core.crypto import generate_key
    from armadok.main import create_app

    app = create_app(
        Settings(data_dir=tmp_path, cookie_secure=False, encryption_key=generate_key())
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        # Without a login a real route answers 401; a route that does not exist answers 404.
        for path in (
            "dns/domains",
            "audit",
            "notifications",
            "nodes/x/traefik/files",
            "nodes/x/certs",
        ):
            assert client.get(f"/api/v1/{path}").status_code == 401, path
