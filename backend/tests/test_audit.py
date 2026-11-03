import logging
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from armadok.core.audit import _entry_hash
from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.audit.routes import _safe_cell

PASSWORD = "correct horse battery"


@pytest.fixture
def env(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False, encryption_key=generate_key()),
            load_registry(("users", "nodes", "audit")),
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    client = TestClient(app)
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    return client, tmp_path


def login(client, user="admin"):
    token = client.post("/api/v1/auth/login", json={"username": user, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def raw_db(tmp_path):
    """A direct database handle with the append-only triggers removed: what someone with write
    access to the file could do."""
    db = sqlite3.connect(tmp_path / "armadok.db")
    db.row_factory = sqlite3.Row
    db.execute("DROP TRIGGER audit_log_no_update")
    db.execute("DROP TRIGGER audit_log_no_delete")
    return db


def make_user(client, admin, name):
    return client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]


def test_chain_is_intact_and_detects_edits_and_deletions(env):
    client, tmp_path = env
    admin = login(client)
    for n in range(3):
        client.post("/api/v1/nodes", headers=admin, json={"name": f"n{n}", "type": "docker"})
    assert client.get("/api/v1/audit/verify", headers=admin).json()["intact"] is True

    db = raw_db(tmp_path)
    target = db.execute(
        "SELECT id FROM audit_log WHERE event = 'node.create' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    original = db.execute("SELECT actor FROM audit_log WHERE id = ?", (target,)).fetchone()[0]
    db.execute("UPDATE audit_log SET actor = 'someone-else' WHERE id = ?", (target,))
    db.commit()
    bad = client.get("/api/v1/audit/verify", headers=admin).json()
    assert bad["intact"] is False and bad["first_bad_id"] == target
    db.execute("UPDATE audit_log SET actor = ? WHERE id = ?", (original, target))
    db.commit()
    assert client.get("/api/v1/audit/verify", headers=admin).json()["intact"] is True

    # Deleting an entry in the middle breaks the chain from the entry that follows it.
    db.execute("DELETE FROM audit_log WHERE id = ?", (target,))
    db.commit()
    assert client.get("/api/v1/audit/verify", headers=admin).json()["intact"] is False


def test_someone_with_the_database_but_not_the_key_cannot_forge_a_valid_chain(env):
    client, tmp_path = env
    admin = login(client)
    client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"})
    db = raw_db(tmp_path)
    first = db.execute("SELECT id FROM audit_log ORDER BY id LIMIT 1").fetchone()
    db.execute("UPDATE audit_log SET actor = 'forged' WHERE id = ?", (first["id"],))
    # The attacker recomputes every hash, but without the signing key (plain SHA-256).
    previous = ""
    for r in db.execute("SELECT * FROM audit_log ORDER BY id").fetchall():
        row = (
            r["id"],
            r["at"],
            r["actor"],
            r["event"],
            r["object_type"],
            r["object_id"],
            r["outcome"],
            r["details"],
        )
        previous = _entry_hash(None, previous, row)
        db.execute("UPDATE audit_log SET entry_hash = ? WHERE id = ?", (previous, r["id"]))
    db.commit()
    assert client.get("/api/v1/audit/verify", headers=admin).json()["intact"] is False


def test_visibility_follows_rights(env):
    client, _ = env
    admin = login(client)
    node = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "mine", "type": "docker"}
    ).json()["id"]
    bob_id = make_user(client, admin, "bob")
    bob = login(client, "bob")
    # No audit right at all: refused, not an empty list.
    assert client.get("/api/v1/audit", headers=bob).status_code == 403
    # Node-scoped right: only entries about that node, and no way to verify the whole chain.
    client.post(
        f"/api/v1/users/{bob_id}/grants",
        headers=admin,
        json={"right": "audit.read", "scope": f"node:{node}"},
    )
    seen = client.get("/api/v1/audit", headers=bob).json()["entries"]
    assert seen and all(e["object_type"] == "node" and e["object_id"] == node for e in seen)
    assert client.get("/api/v1/audit/verify", headers=bob).status_code == 403
    # Platform-wide right sees everything, including account events.
    events = {
        e["event"] for e in client.get("/api/v1/audit?limit=200", headers=admin).json()["entries"]
    }
    assert {"setup.complete", "auth.login", "node.create", "user.create", "grant.add"} <= events


def test_filters_paging_and_account_names(env):
    client, _ = env
    admin = login(client)
    for n in range(5):
        client.post("/api/v1/nodes", headers=admin, json={"name": f"n{n}", "type": "docker"})
    page1 = client.get("/api/v1/audit?event=node.create&limit=2", headers=admin).json()
    assert len(page1["entries"]) == 2 and page1["next_before_id"]
    page2 = client.get(
        f"/api/v1/audit?event=node.create&limit=2&before_id={page1['next_before_id']}",
        headers=admin,
    ).json()
    ids = [e["id"] for e in page1["entries"] + page2["entries"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 4
    assert page1["entries"][0]["actor_name"] == "admin"  # the id is shown as the account's name
    assert client.get("/api/v1/audit?outcome=bogus", headers=admin).status_code == 422
    assert (
        client.get("/api/v1/audit?actor=%25", headers=admin).json()["entries"] == []
    )  # % is literal


def test_export_formats_and_spreadsheet_formula_neutralisation(env):
    client, _ = env
    admin = login(client)
    csv_out = client.get("/api/v1/audit/export?format=csv", headers=admin)
    assert csv_out.headers["content-type"].startswith("text/csv")
    assert csv_out.text.splitlines()[0] == "id,at,actor,event,object_type,object_id,outcome,details"
    assert isinstance(client.get("/api/v1/audit/export", headers=admin).json(), list)
    assert _safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _safe_cell("+1") == "'+1"
    assert _safe_cell("-2") == "'-2" and _safe_cell("@x") == "'@x" and _safe_cell(None) == ""
    assert _safe_cell("\t=cmd") == "'\t=cmd" and _safe_cell("\r=cmd") == "'\r=cmd"
    assert _safe_cell("  =cmd") == "'  =cmd"
    assert client.get("/api/v1/audit/export?format=xml", headers=admin).status_code == 422
    assert client.get("/api/v1/audit").status_code == 401


def test_sql_edits_and_deletes_are_refused_by_the_database_itself(env):
    client, tmp_path = env
    admin = login(client)
    client.post("/api/v1/nodes", headers=admin, json={"name": "n", "type": "docker"})
    db = sqlite3.connect(tmp_path / "armadok.db")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_log SET actor = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit_log")


def test_removing_the_newest_entries_or_leaving_a_gap_is_detected(env):
    client, tmp_path = env
    admin = login(client)
    for n in range(4):
        client.post("/api/v1/nodes", headers=admin, json={"name": f"n{n}", "type": "docker"})
    db = raw_db(tmp_path)
    last = db.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    db.execute("DELETE FROM audit_log WHERE id >= ?", (last - 1,))
    db.commit()
    result = client.get("/api/v1/audit/verify", headers=admin).json()
    assert result["intact"] is False  # the signed head no longer matches the last entry
    db.execute("DELETE FROM audit_log")
    db.commit()
    assert client.get("/api/v1/audit/verify", headers=admin).json()["intact"] is False  # emptied


def test_verify_says_when_nothing_is_signed(tmp_path, caplog):
    import logging as _l

    with caplog.at_level(_l.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path, cookie_secure=False),
            load_registry(("users", "nodes", "audit")),
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    client = TestClient(app)
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    admin = login(client)
    result = client.get("/api/v1/audit/verify", headers=admin).json()
    assert result["intact"] is True and result["signed"] is False


def test_legacy_rows_without_a_hash_are_reported_not_flagged(env):
    client, tmp_path = env
    db = raw_db(tmp_path)
    # Entries written before the chain existed: no hash, and the first ids.
    db.execute("DELETE FROM audit_log")
    db.execute("DELETE FROM audit_head")
    db.execute(
        "INSERT INTO audit_log (id, at, actor, event, outcome, details)"
        " VALUES (1, 't', 'a', 'old', 'success', '{}')"
    )
    db.commit()
    admin = login(client)
    result = client.get("/api/v1/audit/verify", headers=admin).json()
    assert result["intact"] is True and result["legacy_unsigned"] == 1


def test_details_are_clipped_and_exports_are_themselves_audited(env):
    client, tmp_path = env
    admin = login(client)
    from armadok.core.audit import record

    conn = sqlite3.connect(
        tmp_path / "armadok.db",
        factory=__import__("armadok.core.db", fromlist=["Connection"]).Connection,
    )
    conn.row_factory = sqlite3.Row
    record(conn, {"x.y"}, "a", "x.y", details={"long": "z" * 5000, "ok": 1})
    row = conn.execute("SELECT details FROM audit_log WHERE event = 'x.y'").fetchone()
    assert len(row["details"]) < 500
    client.get("/api/v1/audit/export?format=csv", headers=admin)
    events = [
        e["event"] for e in client.get("/api/v1/audit?limit=5", headers=admin).json()["entries"]
    ]
    assert "audit.export" in events
    assert (
        client.get("/api/v1/audit/export?format=csv", headers=admin)
        .headers["content-disposition"]
        .startswith("attachment")
    )
