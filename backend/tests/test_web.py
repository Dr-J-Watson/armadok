from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.module import Module
from armadok.core.registry import Registry
from armadok.main import create_app


def make_client(tmp_path, with_web=True):
    web = tmp_path / "web"
    if with_web:
        (web / "assets").mkdir(parents=True)
        (web / "index.html").write_text("<!doctype html><title>Armadok</title>")
        (web / "assets" / "app-abc123.js").write_text("console.log(1)")
        (web / "favicon.svg").write_text("<svg/>")
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be served")
    settings = Settings(data_dir=tmp_path / "data", web_dir=web)
    return TestClient(create_app(settings, Registry([Module(name="demo")])))


def test_interface_is_served_with_security_headers(tmp_path):
    client = make_client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200 and "Armadok" in page.text
    assert "script-src" not in page.headers["content-security-policy"]
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-cache"


def test_client_side_routes_return_the_app_and_real_files_are_served(tmp_path):
    client = make_client(tmp_path)
    assert "Armadok" in client.get("/nodes/some-id").text
    assert client.get("/favicon.svg").text == "<svg/>"
    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]


def test_api_is_never_cached_and_unknown_api_paths_are_json_404(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/v1/health").headers["cache-control"] == "no-store"
    missing = client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404 and missing.json() == {"error": {"code": "common.not_found"}}


def test_files_outside_the_interface_folder_are_not_served(tmp_path):
    client = make_client(tmp_path)
    for path in (
        "/../secret.txt",
        "/..%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/assets/../../secret.txt",
    ):
        assert "must not be served" not in client.get(path).text


def test_without_a_built_interface_the_hub_still_works(tmp_path):
    client = make_client(tmp_path, with_web=False)
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 404
