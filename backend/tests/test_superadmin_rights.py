import logging
import re

from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.users import setup

PASSWORD = "correct horse battery"


def make(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path / "data", cookie_secure=False, encryption_key=generate_key())
        )
    return app, re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)


def rights_of(app, username):
    conn = app.state.settings.open_db()
    try:
        return {
            r["right"]
            for r in conn.execute(
                "SELECT g.right FROM grants g JOIN users u ON u.id = g.principal_id"
                " WHERE u.username = ?",
                (username,),
            )
        }
    finally:
        conn.close()


def test_a_right_added_after_setup_reaches_the_administrator_once(tmp_path, caplog):
    app, code = make(tmp_path, caplog)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).json()["access_token"]
        admin = {"Authorization": f"Bearer {token}"}
        client.post("/api/v1/users", headers=admin, json={"username": "bob", "password": PASSWORD})
    conn = app.state.settings.open_db()
    # An installation from an older version: a platform right it never had, and none recorded.
    conn.execute("DELETE FROM grants WHERE right = 'vulns.read'")
    conn.execute("DELETE FROM superadmin_rights_applied WHERE right = 'vulns.read'")
    conn.commit()
    setup.ensure_superadmin_rights(conn)
    assert "vulns.read" in rights_of(app, "admin")
    assert "vulns.read" not in rights_of(app, "bob")  # only the installation's administrator
    # The administrator then takes it away on purpose: it is not given back at the next start.
    conn.execute("DELETE FROM grants WHERE right = 'vulns.read'")
    conn.commit()
    setup.ensure_superadmin_rights(conn)
    conn.close()
    assert "vulns.read" not in rights_of(app, "admin")


def test_a_fresh_setup_records_every_right_as_handed_out(tmp_path, caplog):
    app, code = make(tmp_path, caplog)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
    conn = app.state.settings.open_db()
    try:
        applied = {r["right"] for r in conn.execute("SELECT right FROM superadmin_rights_applied")}
    finally:
        conn.close()
    assert applied == set(setup.SUPERADMIN_RIGHTS)
