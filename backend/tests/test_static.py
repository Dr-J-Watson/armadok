"""Traefik's static configuration: read, replace with a kept copy, put back an older one."""
# ruff: noqa: F811

import hashlib

import pytest

from armadok.worker import traefik_ops
from armadok.worker.operation import OperationError
from tests.test_middlewares import folder, hub  # noqa: F401  (fixtures)

STATIC = "entryPoints:\n  web:\n    address: ':80'\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    root = tmp_path / "static"
    root.mkdir()
    (root / "traefik.yml").write_text(STATIC)
    monkeypatch.setenv("ARMADOK_TRAEFIK_STATIC_DIR", str(root))
    return root


def url(node, tail=""):
    return f"/api/v1/nodes/{node}/traefik/static{tail}"


def test_without_a_static_folder_the_worker_says_so_and_refuses_to_write(monkeypatch):
    monkeypatch.delenv("ARMADOK_TRAEFIK_STATIC_DIR", raising=False)
    assert traefik_ops.static_read({}) == {"configured": False}
    with pytest.raises(OperationError) as refused:
        traefik_ops.static_write({"content": "a: 1\n", "expected_sha256": "0" * 64})
    assert refused.value.code == "worker.traefik_not_configured"


def test_the_worker_reads_and_replaces_only_that_file_and_keeps_the_old_one(static_dir):
    read = traefik_ops.static_read({})
    assert read["configured"] and read["name"] == "traefik.yml" and read["content"] == STATIC
    new = STATIC + "api:\n  dashboard: false\n"
    saved = traefik_ops.static_write({"content": new, "expected_sha256": read["sha256"]})
    assert saved["sha256"] == sha(new) and (static_dir / "traefik.yml").read_text() == new
    versions = traefik_ops.static_history_list({})["versions"]
    assert len(versions) == 1
    old = traefik_ops.static_history_read({"version": versions[0]["version"]})
    assert old["content"] == STATIC
    with pytest.raises(OperationError) as stale:  # edited since it was read
        traefik_ops.static_write({"content": new, "expected_sha256": read["sha256"]})
    assert stale.value.code == "worker.conflict"
    assert sorted(p.name for p in static_dir.iterdir() if p.is_file()) == ["traefik.yml"]


def test_a_symlink_in_place_of_the_file_is_refused(static_dir, tmp_path):
    (static_dir / "traefik.yml").unlink()
    secret = tmp_path / "elsewhere.yml"
    secret.write_text("secret: 1\n")
    (static_dir / "traefik.yml").symlink_to(secret)
    with pytest.raises(OperationError):
        traefik_ops.static_read({})
    assert secret.read_text() == "secret: 1\n"


def test_through_the_hub_the_text_is_checked_the_edit_is_audited_and_an_old_copy_comes_back(
    hub,
    static_dir,  # noqa: F811
):
    client, admin, node, _ = hub
    got = client.get(url(node), headers=admin)
    assert got.status_code == 200 and got.json()["content"] == STATIC
    current = got.json()["sha256"]
    for bad in ("a: [unclosed\n", "", "- just\n- a list\n"):
        refused = client.put(
            url(node), headers=admin, json={"content": bad, "expected_sha256": current}
        )
        assert refused.status_code == 422, bad
    assert (static_dir / "traefik.yml").read_text() == STATIC  # nothing was written
    new = STATIC + "log:\n  level: INFO\n"
    ok = client.put(url(node), headers=admin, json={"content": new, "expected_sha256": current})
    assert ok.status_code == 200
    assert (
        client.put(
            url(node), headers=admin, json={"content": new, "expected_sha256": current}
        ).status_code
        == 409
    )
    versions = client.get(url(node, "/history"), headers=admin).json()["versions"]
    old = client.get(url(node, f"/history/{versions[0]['version']}"), headers=admin).json()
    assert old["content"] == STATIC
    log = client.get("/api/v1/audit", headers=admin).text
    assert "traefik.static_write" in log and "traefik.static_read" in log
    assert "INFO" not in log  # the content never reaches the audit log


def test_the_static_right_is_separate_and_sensitive(hub, static_dir):  # noqa: F811
    client, admin, node, _ = hub
    rights = {r["id"]: r for r in client.get("/api/v1/rights", headers=admin).json()}
    assert rights["traefik.static.write"]["sensitive"] is True


def test_no_copy_no_change_and_yml_wins_over_yaml(static_dir, tmp_path):
    current = traefik_ops.static_read({})["sha256"]
    # The place for the old copy is taken by a plain file: the copy cannot be made.
    (static_dir / ".armadok-history").write_text("in the way")
    with pytest.raises(OperationError) as refused:
        traefik_ops.static_write({"content": STATIC + "log: {}\n", "expected_sha256": current})
    assert refused.value.code == "worker.history_failed"
    assert (static_dir / "traefik.yml").read_text() == STATIC  # the file was not touched

    # An unusable traefik.yml is reported as such; traefik.yaml is not quietly used instead.
    (static_dir / "traefik.yml").unlink()
    elsewhere = tmp_path / "other.yml"
    elsewhere.write_text("x: 1\n")
    (static_dir / "traefik.yml").symlink_to(elsewhere)
    (static_dir / "traefik.yaml").write_text("y: 2\n")
    with pytest.raises(OperationError):
        traefik_ops.static_read({})
    (static_dir / "traefik.yml").unlink()
    assert traefik_ops.static_read({})["name"] == "traefik.yaml"  # only when .yml is missing


def test_a_stale_edit_leaves_no_copy_behind(static_dir):
    with pytest.raises(OperationError) as stale:
        traefik_ops.static_write({"content": "a: 1\n", "expected_sha256": "0" * 64})
    assert stale.value.code == "worker.conflict"
    assert not (static_dir / ".armadok-history").exists()


def test_a_text_too_long_in_bytes_is_refused_before_it_is_parsed(hub, static_dir):
    client, admin, node, _ = hub
    current = client.get(url(node), headers=admin).json()["sha256"]
    long = "a: " + "é" * 70000  # under the limit in characters, over it in bytes
    got = client.put(url(node), headers=admin, json={"content": long, "expected_sha256": current})
    assert got.status_code == 422
