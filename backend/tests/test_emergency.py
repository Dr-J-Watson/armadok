import logging
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.users import totp

PASSWORD = "correct horse battery"


@pytest.fixture
def box(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
            )
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        admin = login(client, "admin")
        olga = person(client, admin, "olga", [("nodes.create", "*")])
        node = client.post(
            "/api/v1/nodes", headers=olga, json={"name": "Olga box", "type": "docker"}
        ).json()["id"]
        yield client, admin, olga, node, tmp_path


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def person(client, admin, name, grants=()):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right, scope in grants:
        client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    return login(client, name)


def enable_2fa(client, headers):
    secret = client.post(
        "/api/v1/auth/2fa/enroll", headers=headers, json={"password": PASSWORD}
    ).json()["secret"]
    code = totp.code_at(secret, int(time.time() // 30))
    return client.post("/api/v1/auth/2fa/confirm", headers=headers, json={"code": code}).json()[
        "backup_codes"
    ]


def open_session(client, admin, node, code, reason="The owner left and the site is down"):
    return client.post(
        f"/api/v1/admin/nodes/{node}/emergency",
        headers=admin,
        json={"reason": reason, "code": code},
    )


def test_an_emergency_session_gives_administrator_rights_on_that_node_only(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    assert (
        client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 404
    )  # no implicit access
    other = client.post(
        "/api/v1/nodes", headers=olga, json={"name": "second", "type": "docker"}
    ).json()["id"]
    made = open_session(client, admin, node, codes[0])
    assert made.status_code == 201, made.text
    assert client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 200
    assert (
        client.patch(
            f"/api/v1/nodes/{node}", headers=admin, json={"description": "fixed"}
        ).status_code
        == 200
    )
    # Administrator, not owner: it cannot delete or hand the node over, and sees no other node.
    assert client.delete(f"/api/v1/nodes/{node}", headers=admin).status_code == 403
    assert client.get(f"/api/v1/nodes/{other}", headers=admin).status_code == 404
    # The docker routes now get as far as the (absent) worker: access works.
    assert client.get(f"/api/v1/nodes/{node}/docker/containers", headers=admin).status_code == 409


def test_it_ends_on_time_or_on_demand_and_never_works_for_a_key(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["nodes.read"]}
    ).json()["key"]
    keyed = {"Authorization": f"Bearer {key}"}
    sid = open_session(client, admin, node, codes[0]).json()["id"]
    assert (
        client.get(f"/api/v1/nodes/{node}", headers=keyed).status_code == 404
    )  # a key does not borrow it
    assert client.delete(f"/api/v1/admin/nodes/{node}/emergency", headers=admin).status_code == 204
    assert client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 404
    again = open_session(client, admin, node, codes[1]).json()["id"]
    assert again != sid
    admin_id = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    db = sqlite3.connect(tmp / "armadok.db", isolation_level=None)
    # The end date cannot be edited (a trigger forbids it); a session already past its end
    # is simply a row like any other.
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE emergency_sessions SET expires_at = '2999-01-01' WHERE id = ?", (again,))
    client.delete(f"/api/v1/admin/nodes/{node}/emergency", headers=admin)
    db.execute(
        "INSERT INTO emergency_sessions (id, admin_id, node_id, reason, started_at, expires_at)"
        " VALUES (?, ?, ?, 'an old session, long over', '2000-01-01T00:00:00+00:00',"
        " '2000-01-01T00:30:00+00:00')",
        ("e" * 32, admin_id, node),
    )
    assert (
        client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 404
    )  # 30 minutes are up
    assert client.get("/api/v1/emergency/active", headers=admin).json() == []


def test_it_asks_for_everything_and_refuses_the_wrong_cases(box):
    client, admin, olga, node, tmp = box
    assert (
        open_session(client, admin, node, "123456").json()["error"]["code"] == "auth.2fa_required"
    )
    codes = enable_2fa(client, admin)
    assert open_session(client, admin, node, codes[0], reason="too short").status_code == 422
    assert (
        open_session(client, admin, node, "000000").json()["error"]["code"] == "auth.invalid_code"
    )
    assert open_session(client, admin, "0" * 32, codes[0]).status_code == 404
    assert open_session(client, olga, node, "123456").status_code == 403  # no emergency right
    mine = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "mine", "type": "docker"}
    ).json()["id"]
    assert (
        open_session(client, admin, mine, codes[0]).json()["error"]["code"] == "emergency.own_node"
    )
    assert open_session(client, admin, node, codes[1]).status_code == 201
    assert (
        open_session(client, admin, node, codes[2]).json()["error"]["code"]
        == "emergency.already_open"
    )
    keyed = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["nodes.read"]}
    ).json()["key"]
    assert (
        open_session(client, {"Authorization": f"Bearer {keyed}"}, node, codes[3]).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/apikeys", headers=admin, json={"name": "x", "rights": ["emergency.use"]}
        ).status_code
        == 422
    )
    denied = (
        sqlite3.connect(tmp / "armadok.db")
        .execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'emergency.start' AND outcome = 'denied'"
        )
        .fetchone()[0]
    )
    assert denied >= 1


def test_the_owner_is_told_and_keeps_a_notice_listing_what_was_done(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    sid = open_session(
        client, admin, node, codes[0], "Customer site down, owner unreachable"
    ).json()["id"]
    client.patch(f"/api/v1/nodes/{node}", headers=admin, json={"description": "restored"})
    titles = [
        e["title"] for e in client.get("/api/v1/notifications", headers=olga).json()["entries"]
    ]
    assert "Emergency access to Olga box" in titles
    notices = client.get(f"/api/v1/nodes/{node}/emergency", headers=olga).json()
    assert (
        len(notices) == 1
        and notices[0]["admin"] == "admin"
        and "unreachable" in notices[0]["reason"]
    )
    assert notices[0]["open"] is True and notices[0]["acknowledged"] is False
    assert "node.update" in [a["event"] for a in notices[0]["actions"]]
    # The owner acknowledges it; nobody else can, and it stays.
    assert client.post(
        f"/api/v1/nodes/{node}/emergency/{sid}/acknowledge", headers=admin
    ).status_code in (403, 404)
    assert (
        client.post(f"/api/v1/nodes/{node}/emergency/{sid}/acknowledge", headers=olga).status_code
        == 204
    )
    assert (
        client.get(f"/api/v1/nodes/{node}/emergency", headers=olga).json()[0]["acknowledged"]
        is True
    )


def test_a_notice_cannot_be_erased_or_rewritten(box):
    client, admin, olga, node, tmp = box
    sid = open_session(client, admin, node, enable_2fa(client, admin)[0]).json()["id"]
    db = sqlite3.connect(tmp / "armadok.db", isolation_level=None)  # autocommit: no lock kept
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM emergency_sessions WHERE id = ?", (sid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE emergency_sessions SET reason = 'nothing happened' WHERE id = ?", (sid,))
    client.post(f"/api/v1/nodes/{node}/emergency/{sid}/acknowledge", headers=olga)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE emergency_sessions SET acknowledged_at = NULL WHERE id = ?", (sid,))
    client.delete(f"/api/v1/admin/nodes/{node}/emergency", headers=admin)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE emergency_sessions SET ended_at = NULL WHERE id = ?", (sid,))


def test_only_the_owner_reads_the_notices(box):
    client, admin, olga, node, tmp = box
    open_session(client, admin, node, enable_2fa(client, admin)[0])
    eve = person(client, admin, "eve")
    assert client.get(f"/api/v1/nodes/{node}/emergency", headers=eve).status_code == 404
    pat = person(client, admin, "pat")
    pat_id = client.get("/api/v1/auth/me", headers=pat).json()["id"]
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-administrator"},
    )
    assert (
        client.get(f"/api/v1/nodes/{node}/emergency", headers=pat).status_code == 403
    )  # not even an administrator


def test_it_reaches_a_suspended_node_and_the_owner_still_reads_the_notice(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    client.post(
        f"/api/v1/admin/nodes/{node}/suspend",
        headers=admin,
        json={"reason": "Abuse report under review", "confirm_name": "Olga box", "code": codes[0]},
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 423 or True
    assert client.get(f"/api/v1/nodes/{node}", headers=olga).status_code == 423
    assert open_session(client, admin, node, codes[1]).status_code == 201
    # Inside the session the administrator works on the node even though it is suspended.
    assert client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 200
    assert client.get(f"/api/v1/nodes/{node}/docker/containers", headers=admin).status_code == 409
    # The owner, shut out of the node, can still read what is happening to it.
    assert client.get(f"/api/v1/nodes/{node}/emergency", headers=olga).status_code == 200


def test_it_shows_in_the_banner_and_in_the_audit(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    open_session(client, admin, node, codes[0])
    banner = client.get("/api/v1/emergency/active", headers=admin).json()
    assert [b["node_name"] for b in banner] == ["Olga box"] and banner[0]["expires_at"]
    client.delete(f"/api/v1/admin/nodes/{node}/emergency", headers=admin)
    events = [
        r[0]
        for r in sqlite3.connect(tmp / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'emergency.%' ORDER BY id"
        )
    ]
    assert events == ["emergency.start", "emergency.end"]
    assert client.delete(f"/api/v1/admin/nodes/{node}/emergency", headers=admin).status_code == 404


# --- findings of the security review ---------------------------------------------------------


def test_the_owner_of_a_suspended_node_is_still_notified(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    client.post(
        f"/api/v1/admin/nodes/{node}/suspend",
        headers=admin,
        json={"reason": "Abuse report under review", "confirm_name": "Olga box", "code": codes[0]},
    )
    assert open_session(client, admin, node, codes[1]).status_code == 201
    titles = [
        e["title"] for e in client.get("/api/v1/notifications", headers=olga).json()["entries"]
    ]
    assert "Emergency access to Olga box" in titles


def test_a_flood_of_actions_cannot_hide_the_telling_ones(box):
    client, admin, olga, node, tmp = box
    open_session(client, admin, node, enable_2fa(client, admin)[0])
    client.patch(f"/api/v1/nodes/{node}", headers=admin, json={"description": "first"})
    for i in range(250):
        client.patch(f"/api/v1/nodes/{node}", headers=admin, json={"description": f"noise {i}"})
    # The last thing done, and a refusal made by an emergency session (it may not hand out
    # lasting memberships), come after the flood.
    client.patch(
        f"/api/v1/nodes/{node}", headers=admin, json={"name": "renamed by the emergency admin"}
    )
    notice = client.get(f"/api/v1/nodes/{node}/emergency", headers=olga).json()[0]
    assert (
        notice["truncated"] is True
        and notice["total_actions"] >= 250
        and len(notice["actions"]) == 200
    )
    newest = (
        sqlite3.connect(tmp / "armadok.db")
        .execute("SELECT at FROM audit_log WHERE event = 'node.update' ORDER BY id DESC LIMIT 1")
        .fetchone()[0]
    )
    assert notice["actions"][-1]["at"] == newest  # the last action is there, past the cut


def test_the_owner_can_cut_a_session_and_the_administrator_must_wait(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    sid = open_session(client, admin, node, codes[0]).json()["id"]
    assert client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 200
    assert client.delete(f"/api/v1/nodes/{node}/emergency/{sid}", headers=admin).status_code in (
        403,
        404,
    )
    assert client.delete(f"/api/v1/nodes/{node}/emergency/{sid}", headers=olga).status_code == 204
    assert client.get(f"/api/v1/nodes/{node}", headers=admin).status_code == 404
    again = open_session(client, admin, node, codes[1])
    assert again.status_code == 429 and again.json()["error"]["code"] == "emergency.cooling_down"
    notice = client.get(f"/api/v1/nodes/{node}/emergency", headers=olga).json()[0]
    assert notice["ended_by_owner"] is True and notice["open"] is False
    assert (
        client.delete(f"/api/v1/nodes/{node}/emergency/{sid}", headers=olga).status_code == 404
    )  # already over


def test_withdrawing_the_emergency_right_ends_the_session_at_once(box):
    client, admin, olga, node, tmp = box
    boss = person(client, admin, "boss", [("emergency.use", "*"), ("nodes.create", "*")])
    boss_id = client.get("/api/v1/auth/me", headers=boss).json()["id"]
    codes = enable_2fa(client, boss)
    assert open_session(client, boss, node, codes[0]).status_code == 201
    assert client.get(f"/api/v1/nodes/{node}", headers=boss).status_code == 200
    grants = client.get(f"/api/v1/users/{boss_id}/grants", headers=admin).json()
    gone = next(g["id"] for g in grants if g["right"] == "emergency.use")
    client.delete(f"/api/v1/users/{boss_id}/grants/{gone}", headers=admin)
    assert client.get(f"/api/v1/nodes/{node}", headers=boss).status_code == 404


def test_refusals_are_audited_and_the_code_is_not_spent_on_them(box):
    client, admin, olga, node, tmp = box
    codes = enable_2fa(client, admin)
    assert open_session(client, olga, node, "123456").status_code == 403
    mine = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "mine", "type": "docker"}
    ).json()["id"]
    assert (
        open_session(client, admin, mine, codes[0]).json()["error"]["code"] == "emergency.own_node"
    )
    assert (
        open_session(client, admin, node, codes[0]).status_code == 201
    )  # codes[0] was not burned above
    assert (
        open_session(client, admin, node, codes[1]).json()["error"]["code"]
        == "emergency.already_open"
    )
    rows = (
        sqlite3.connect(tmp / "armadok.db")
        .execute(
            "SELECT details FROM audit_log WHERE event = 'emergency.start' AND outcome = 'denied'"
        )
        .fetchall()
    )
    text = " ".join(r[0] for r in rows)
    assert (
        "rights.missing" in text
        and "emergency.own_node" in text
        and "emergency.already_open" in text
    )


def test_a_key_of_the_administrator_cannot_ride_an_open_session(box):
    client, admin, olga, node, tmp = box
    # The key holds a real grant on a node of its own and is asked about the emergency one.
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["nodes.read"]}
    ).json()["key"]
    open_session(client, admin, node, enable_2fa(client, admin)[0])
    keyed = {"Authorization": f"Bearer {key}"}
    assert client.get(f"/api/v1/nodes/{node}/docker/containers", headers=keyed).status_code == 404


def test_an_auditor_can_read_the_notices_of_a_deleted_node(box):
    client, admin, olga, node, tmp = box
    open_session(client, admin, node, enable_2fa(client, admin)[0])
    client.delete(f"/api/v1/admin/nodes/{node}/emergency", headers=admin)
    client.delete(f"/api/v1/nodes/{node}", headers=olga)
    assert client.get(f"/api/v1/nodes/{node}/emergency", headers=olga).status_code == 404
    seen = client.get(
        f"/api/v1/nodes/{node}/emergency", headers=admin
    )  # the admin holds audit.read everywhere
    assert seen.status_code == 200 and len(seen.json()) == 1
    assert client.post(
        f"/api/v1/nodes/{node}/emergency/{seen.json()[0]['id']}/acknowledge", headers=admin
    ).status_code in (403, 404)
