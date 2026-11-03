from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.db import Migration, apply_migrations, connect
from armadok.core.module import Module
from armadok.core.registry import Registry
from armadok.main import create_app


def test_health_and_modules(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), Registry([Module(name="demo")]))
    client = TestClient(app)
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/api/v1/modules").json() == {"modules": ["demo"]}


def test_migrations_are_applied_once(tmp_path):
    conn = connect(tmp_path / "t.db")
    migration = Migration("demo-0001", "CREATE TABLE demo (id INTEGER PRIMARY KEY);")
    assert apply_migrations(conn, [migration]) == ["demo-0001"]
    assert apply_migrations(conn, [migration]) == []
