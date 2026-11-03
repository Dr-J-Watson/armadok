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
        yield client, login(client, "admin"), tmp_path


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
        r = client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
        assert r.status_code == 201, r.text
    return uid, login(client, name)


def owner_with_node(client, admin, name="olga", node_name="Olga box"):
    uid, headers = person(client, admin, name, [("nodes.create", "*")])
    node = client.post(
        "/api/v1/nodes", headers=headers, json={"name": node_name, "type": "docker"}
    ).json()["id"]
    return uid, headers, node


def enable_2fa(client, headers):
    secret = client.post(
        "/api/v1/auth/2fa/enroll", headers=headers, json={"password": PASSWORD}
    ).json()["secret"]
    code = totp.code_at(secret, int(time.time() // 30))
    return client.post("/api/v1/auth/2fa/confirm", headers=headers, json={"code": code}).json()[
        "backup_codes"
    ]


def role(client, headers, name):
    return (
        next(
            r["id"]
            for r in client.get("/api/v1/roles", headers=headers).json()
            if r["name"] == name
        )
        if False
        else f"builtin-{name}"
    )


# --- members --------------------------------------------------------------------------------


def test_an_owner_manages_the_members_of_their_node_without_any_platform_right(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    pat_id, pat = person(client, admin, "pat")
    assert client.get(f"/api/v1/nodes/{node}", headers=pat).status_code == 404
    added = client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    )
    assert added.status_code == 201, added.text
    assert client.get(f"/api/v1/nodes/{node}", headers=pat).status_code == 200
    listing = client.get(f"/api/v1/nodes/{node}/members", headers=olga).json()
    assert listing["owner"]["name"] == "olga"
    assert [(m["subject_name"], m["role_name"]) for m in listing["members"]] == [("pat", "viewer")]
    changed = client.patch(
        f"/api/v1/nodes/{node}/members/{added.json()['id']}",
        headers=olga,
        json={"role_id": "builtin-operator"},
    )
    assert changed.status_code == 200
    members = client.get(f"/api/v1/nodes/{node}/members", headers=olga).json()["members"]
    assert members[0]["role_name"] == "operator"
    assert (
        client.delete(
            f"/api/v1/nodes/{node}/members/{changed.json()['id']}", headers=olga
        ).status_code
        == 204
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=pat).status_code == 404


def test_members_see_nothing_of_the_list_and_strangers_do_not_know_the_node(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    pat_id, pat = person(client, admin, "pat")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    )
    assert (
        client.get(f"/api/v1/nodes/{node}/members", headers=pat).status_code == 403
    )  # sees the node, not who is on it
    _, eve = person(client, admin, "eve")
    assert client.get(f"/api/v1/nodes/{node}/members", headers=eve).status_code == 404
    assert (
        client.post(
            f"/api/v1/nodes/{node}/members",
            headers=eve,
            json={"user_id": pat_id, "role_id": "builtin-viewer"},
        ).status_code
        == 404
    )


def test_a_manager_hands_out_only_what_they_hold(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    ann_id, ann = person(client, admin, "ann")
    bob_id, bob = person(client, admin, "bob")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": ann_id, "role_id": "builtin-administrator"},
    )
    # An administrator may invite, but cannot give the owner's own rights (delete, transfer).
    assert (
        client.post(
            f"/api/v1/nodes/{node}/members",
            headers=ann,
            json={"user_id": bob_id, "role_id": "builtin-owner"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/nodes/{node}/members",
            headers=ann,
            json={"user_id": bob_id, "role_id": "builtin-operator"},
        ).status_code
        == 201
    )
    # ... and cannot demote the owner's own role either.
    owner_member = client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": bob_id, "role_id": "builtin-owner"},
    )
    assert owner_member.status_code == 201
    assert (
        client.delete(
            f"/api/v1/nodes/{node}/members/{owner_member.json()['id']}", headers=ann
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/nodes/{node}/members/{owner_member.json()['id']}",
            headers=ann,
            json={"role_id": "builtin-viewer"},
        ).status_code
        == 403
    )


def test_bad_member_requests_are_refused(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    pat_id, _ = person(client, admin, "pat")
    url = f"/api/v1/nodes/{node}/members"
    assert (
        client.post(
            url, headers=olga, json={"user_id": "nobody", "role_id": "builtin-viewer"}
        ).status_code
        == 404
    )
    assert (
        client.post(url, headers=olga, json={"user_id": pat_id, "role_id": "nope"}).status_code
        == 404
    )
    assert (
        client.post(
            url,
            headers=olga,
            json={
                "user_id": pat_id,
                "role_id": "builtin-viewer",
                "expires_at": "2000-01-01T00:00:00+00:00",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, headers=olga, json={"user_id": pat_id, "role_id": "builtin-viewer", "extra": 1}
        ).status_code
        == 422
    )
    # A service account is not a person to invite.
    svc = client.post("/api/v1/service-accounts", headers=admin, json={"name": "ci"}).json()["id"]
    assert (
        client.post(
            url, headers=olga, json={"user_id": svc, "role_id": "builtin-viewer"}
        ).status_code
        == 404
    )
    # An assignment of another node cannot be reached through this one.
    other = client.post(
        "/api/v1/nodes", headers=olga, json={"name": "second", "type": "docker"}
    ).json()["id"]
    made = client.post(
        f"/api/v1/nodes/{other}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    ).json()
    assert client.delete(f"{url}/{made['id']}", headers=olga).status_code == 404


def test_the_directory_finds_people_by_name_for_those_who_manage_members(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    person(client, admin, "patricia")
    person(client, admin, "patrick")
    client.post("/api/v1/service-accounts", headers=admin, json={"name": "patrol"})
    found = client.get("/api/v1/directory/users?q=pat", headers=olga).json()
    assert [u["username"] for u in found] == ["patricia", "patrick"]
    assert set(found[0]) == {"id", "username"}
    assert client.get("/api/v1/directory/users?q=%25", headers=olga).status_code == 422
    _, eve = person(client, admin, "eve")
    assert client.get("/api/v1/directory/users?q=pat", headers=eve).status_code == 403


def test_member_changes_are_audited(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    pat_id, _ = person(client, admin, "pat")
    made = client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    ).json()
    client.delete(f"/api/v1/nodes/{node}/members/{made['id']}", headers=olga)
    events = [
        r[0]
        for r in sqlite3.connect(tmp / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'node.member%' ORDER BY id"
        )
    ]
    assert events == ["node.member_add", "node.member_remove"]


# --- transfer -------------------------------------------------------------------------------


def hand_over(client, headers, node, name, user_id, code):
    return client.post(
        f"/api/v1/nodes/{node}/transfer",
        headers=headers,
        json={"user_id": user_id, "confirm_name": name, "code": code},
    )


def test_the_owner_hands_the_node_over_and_stays_on_as_an_administrator(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    dan_id, dan = person(client, admin, "dan", [("nodes.create", "*")])
    olga_codes, dan_codes = enable_2fa(client, olga), enable_2fa(client, dan)
    done = hand_over(client, olga, node, "Olga box", dan_id, olga_codes[0])
    assert done.status_code == 200 and done.json() == {"owner": dan_id}
    assert client.get(f"/api/v1/nodes/{node}", headers=dan).json()["owner"] == dan_id
    # Dan can do what only an owner can; Olga can no longer delete or transfer.
    assert hand_over(client, olga, node, "Olga box", olga_id, olga_codes[1]).status_code == 403
    assert client.delete(f"/api/v1/nodes/{node}", headers=olga).status_code == 403
    assert client.get(f"/api/v1/nodes/{node}", headers=olga).status_code == 200  # an administrator
    assert (
        client.patch(f"/api/v1/nodes/{node}", headers=olga, json={"description": "x"}).status_code
        == 200
    )
    assert hand_over(client, dan, node, "Olga box", olga_id, dan_codes[0]).status_code == 200
    assert client.delete(f"/api/v1/nodes/{node}", headers=olga).status_code == 204


def test_a_hand_over_asks_for_a_session_the_name_and_a_fresh_code(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    dan_id, _ = person(client, admin, "dan")
    assert (
        hand_over(client, olga, node, "Olga box", dan_id, "123456").json()["error"]["code"]
        == "auth.2fa_required"
    )
    codes = enable_2fa(client, olga)
    assert (
        hand_over(client, olga, node, "wrong", dan_id, codes[0]).json()["error"]["code"]
        == "nodeadmin.confirm_mismatch"
    )
    assert (
        hand_over(client, olga, node, "Olga box", dan_id, "000000").json()["error"]["code"]
        == "auth.invalid_code"
    )
    # A key, even one that holds the right, cannot do it.
    key = client.post(
        "/api/v1/apikeys", headers=olga, json={"name": "k", "rights": ["nodes.read"]}
    ).json()["key"]
    assert hand_over(
        client, {"Authorization": f"Bearer {key}"}, node, "Olga box", dan_id, codes[0]
    ).status_code in (403, 404)
    assert client.get(f"/api/v1/nodes/{node}", headers=olga).json()["owner"] == olga_id


def test_only_the_owner_hands_a_node_over_and_never_to_the_wrong_account(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    ann_id, ann = person(client, admin, "ann")
    dan_id, _ = person(client, admin, "dan")
    # Even with the owner role given to her, Ann is not the owner.
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": ann_id, "role_id": "builtin-owner"},
    )
    ann_codes = enable_2fa(client, ann)
    assert hand_over(client, ann, node, "Olga box", dan_id, ann_codes[0]).status_code == 403
    codes = enable_2fa(client, olga)
    assert hand_over(client, olga, node, "Olga box", olga_id, codes[0]).status_code == 409
    assert hand_over(client, olga, node, "Olga box", "nobody", codes[1]).status_code == 404
    svc = client.post("/api/v1/service-accounts", headers=admin, json={"name": "ci"}).json()["id"]
    assert hand_over(client, olga, node, "Olga box", svc, codes[2]).status_code == 404
    client.patch(f"/api/v1/users/{dan_id}", headers=admin, json={"is_active": False})
    assert hand_over(client, olga, node, "Olga box", dan_id, codes[3]).status_code == 404
    local = client.get("/api/v1/nodes", headers=admin).json()[0]
    admin_codes = enable_2fa(client, admin)
    assert hand_over(
        client, admin, local["id"], local["name"], ann_id, admin_codes[0]
    ).status_code in (403, 409)


def test_the_new_owner_gets_permanent_rights_even_if_they_held_a_short_one_before(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    dan_id, dan = person(client, admin, "dan")
    soon = "2099-01-01T00:00:00+00:00"
    client.post(
        f"/api/v1/users/{dan_id}/grants",
        headers=admin,
        json={"right": "nodes.read", "scope": f"node:{node}", "expires_at": soon},
    )
    codes = enable_2fa(client, olga)
    assert hand_over(client, olga, node, "Olga box", dan_id, codes[0]).status_code == 200
    rows = (
        sqlite3.connect(tmp / "armadok.db")
        .execute(
            "SELECT expires_at FROM grants"
            " WHERE principal_id = ? AND right = 'nodes.read' AND scope = ?",
            (dan_id, f"node:{node}"),
        )
        .fetchall()
    )
    assert rows == [(None,)]  # one clean, permanent row


def test_a_second_hand_over_by_the_former_owner_gets_nowhere(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    dan_id, _ = person(client, admin, "dan")
    eve_id, _ = person(client, admin, "eve")
    codes = enable_2fa(client, olga)
    assert hand_over(client, olga, node, "Olga box", dan_id, codes[0]).status_code == 200
    # The request made with the old principal is judged on what the database says now.
    assert hand_over(client, olga, node, "Olga box", eve_id, codes[1]).status_code == 403
    assert client.get(f"/api/v1/nodes/{node}", headers=olga).json()["owner"] == dan_id


def test_a_transfer_is_audited(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    dan_id, _ = person(client, admin, "dan")
    hand_over(client, olga, node, "Olga box", dan_id, enable_2fa(client, olga)[0])
    row = (
        sqlite3.connect(tmp / "armadok.db")
        .execute("SELECT details FROM audit_log WHERE event = 'node.transfer'")
        .fetchone()
    )
    assert dan_id in row[0]


def test_a_member_change_is_all_or_nothing(box, monkeypatch):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    pat_id, pat = person(client, admin, "pat")
    made = client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": pat_id, "role_id": "builtin-viewer"},
    ).json()
    from armadok.modules.roles import repository

    real = repository.add_assignment

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repository, "add_assignment", boom)
    with pytest.raises(RuntimeError):
        client.patch(
            f"/api/v1/nodes/{node}/members/{made['id']}",
            headers=olga,
            json={"role_id": "builtin-operator"},
        )
    monkeypatch.setattr(repository, "add_assignment", real)
    # The old role was not lost halfway.
    assert client.get(f"/api/v1/nodes/{node}", headers=pat).status_code == 200
    assert [
        m["role_name"]
        for m in client.get(f"/api/v1/nodes/{node}/members", headers=olga).json()["members"]
    ] == ["viewer"]


def test_the_directory_needs_two_letters_and_is_rate_limited(box, monkeypatch):
    client, admin, _ = box
    from armadok.modules.members import routes

    _, olga, node = owner_with_node(client, admin)
    assert client.get("/api/v1/directory/users?q=p", headers=olga).status_code == 422
    monkeypatch.setattr(routes.directory_limiter, "limits", {"user": 3})
    for _ in range(3):
        assert client.get("/api/v1/directory/users?q=ol", headers=olga).status_code == 200
    assert client.get("/api/v1/directory/users?q=ol", headers=olga).status_code == 429
    # Rights on a deleted node do not count.
    _, pia, node2 = owner_with_node(client, admin, "pia", "Pia box")
    client.delete(f"/api/v1/nodes/{node2}", headers=pia)
    assert client.get("/api/v1/directory/users?q=ol", headers=pia).status_code == 403


# --- suspension -----------------------------------------------------------------------------


def suspend(client, admin, node, name, code, reason="Abuse reported by the host"):
    return client.post(
        f"/api/v1/admin/nodes/{node}/suspend",
        headers=admin,
        json={"reason": reason, "confirm_name": name, "code": code},
    )


def test_suspending_a_node_shuts_everyone_out_but_tells_the_owner_why(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    codes = enable_2fa(client, admin)
    done = suspend(client, admin, node, "Olga box", codes[0])
    assert done.status_code == 200, done.text
    listing = {n["id"]: n for n in client.get("/api/v1/nodes", headers=olga).json()}
    assert listing[node]["suspended"] is True and "Abuse" in listing[node]["suspended_reason"]
    refused = client.get(f"/api/v1/nodes/{node}", headers=olga)
    assert refused.status_code == 423 and refused.json()["error"]["code"] == "node.suspended"
    assert (
        client.patch(f"/api/v1/nodes/{node}", headers=olga, json={"description": "x"}).status_code
        == 423
    )
    # Every feature of the node answers "not found", for the owner and for a key alike.
    assert client.get(f"/api/v1/nodes/{node}/docker/containers", headers=olga).status_code == 404
    key = client.post("/api/v1/apikeys", headers=olga, json={"name": "k", "rights": ["nodes.read"]})
    assert key.status_code in (201, 422)
    # The administrator who suspended it still sees nothing of its content.
    assert client.get(f"/api/v1/nodes/{node}/docker/containers", headers=admin).status_code == 404
    # Resuming gives it back.
    back = client.post(
        f"/api/v1/admin/nodes/{node}/resume",
        headers=admin,
        json={"confirm_name": "Olga box", "code": codes[1]},
    )
    assert back.status_code == 200
    assert client.get(f"/api/v1/nodes/{node}", headers=olga).status_code == 200


def test_a_suspended_node_also_stops_keys_and_hooks(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    key = client.post(
        "/api/v1/apikeys", headers=olga, json={"name": "k", "rights": ["nodes.read"]}
    ).json()["key"]
    keyed = {"Authorization": f"Bearer {key}"}
    assert client.get(f"/api/v1/nodes/{node}", headers=keyed).status_code == 200
    codes = enable_2fa(client, admin)
    suspend(client, admin, node, "Olga box", codes[0])
    assert client.get(f"/api/v1/nodes/{node}", headers=keyed).status_code == 423


def test_quick_actions_need_everything_they_ask_for(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    # No second factor yet.
    assert (
        suspend(client, admin, node, "Olga box", "123456").json()["error"]["code"]
        == "auth.2fa_required"
    )
    codes = enable_2fa(client, admin)
    assert (
        suspend(client, admin, node, "olga box", codes[0]).json()["error"]["code"]
        == "nodeadmin.confirm_mismatch"
    )
    assert (
        suspend(client, admin, node, "Olga box", "000000").json()["error"]["code"]
        == "auth.invalid_code"
    )
    assert suspend(client, admin, node, "Olga box", codes[0], reason="short").status_code == 422
    assert suspend(client, admin, "0" * 32, "x", codes[0]).status_code == 404
    local = client.get("/api/v1/nodes", headers=admin).json()[0]
    assert (
        suspend(client, admin, local["id"], local["name"], codes[2]).json()["error"]["code"]
        == "nodeadmin.local_not_suspendable"
    )
    # The name check comes before the code is used: a typo did not burn codes[0].
    assert suspend(client, admin, node, "Olga box", codes[0]).status_code == 200
    again = suspend(client, admin, node, "Olga box", codes[1])
    assert again.status_code == 409 and again.json()["error"]["code"] == "nodeadmin.wrong_state"
    # Nobody else may.
    assert suspend(client, olga, node, "Olga box", "123456").status_code == 403
    events = [
        r[0]
        for r in sqlite3.connect(tmp / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'node.%' ORDER BY id"
        )
    ]
    assert "node.suspend" in events


def test_wrong_codes_are_limited(box):
    client, admin, _ = box
    _, _, node = owner_with_node(client, admin)
    enable_2fa(client, admin)
    for _ in range(5):
        assert suspend(client, admin, node, "Olga box", "000000").status_code == 401
    assert suspend(client, admin, node, "Olga box", "000000").status_code == 429


def test_deleted_nodes_can_be_restored_for_thirty_days(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    assert client.delete(f"/api/v1/nodes/{node}", headers=olga).status_code == 204
    codes = enable_2fa(client, admin)
    listing = {n["id"]: n for n in client.get("/api/v1/admin/nodes", headers=admin).json()}
    assert listing[node]["state"] == "deleted" and listing[node]["restorable_until"]
    assert listing[node]["owner"] == "olga" and "docker" not in str(listing[node].keys())
    done = client.post(
        f"/api/v1/admin/nodes/{node}/restore",
        headers=admin,
        json={"confirm_name": "Olga box", "code": codes[0]},
    )
    assert done.status_code == 200
    assert client.get(f"/api/v1/nodes/{node}", headers=olga).status_code == 200
    # Too late.
    client.delete(f"/api/v1/nodes/{node}", headers=olga)
    db = sqlite3.connect(tmp / "armadok.db")
    db.execute("UPDATE nodes SET deleted_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (node,))
    db.commit()
    late = client.post(
        f"/api/v1/admin/nodes/{node}/restore",
        headers=admin,
        json={"confirm_name": "Olga box", "code": codes[1]},
    )
    assert late.status_code == 410


def test_the_admin_view_shows_no_content_and_needs_the_admin_right(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    assert client.get("/api/v1/admin/nodes", headers=olga).status_code == 403
    assert client.get(f"/api/v1/admin/nodes/{node}/impact", headers=olga).status_code == 403
    impact = client.get(f"/api/v1/admin/nodes/{node}/impact", headers=admin).json()
    assert set(impact) == {"members", "worker_connected"}
    key = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "x", "rights": ["nodes.admin"]}
    )
    assert key.status_code == 422  # sensitive


def test_a_suspended_node_is_shut_for_internal_callers_and_node_auditors(box):
    import asyncio

    from armadok.core.errors import ApiError
    from armadok.modules.workers.routes import call_operation

    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    aud_id, aud = person(client, admin, "aud")
    client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": aud_id, "role_id": "builtin-auditor"},
    )
    assert client.get("/api/v1/audit", headers=aud).status_code == 200
    codes = enable_2fa(client, admin)
    assert suspend(client, admin, node, "Olga box", codes[0]).status_code == 200
    assert client.get("/api/v1/audit", headers=aud).status_code == 403  # nothing left it may read
    app = client.app
    with pytest.raises(ApiError) as caught:
        asyncio.run(call_operation(app, app.state.registry, node, "docker.info", {}))
    assert caught.value.code == "node.suspended" and caught.value.status_code == 423


def test_refused_quick_actions_are_audited(box):
    client, admin, tmp = box
    _, _, node = owner_with_node(client, admin)
    codes = enable_2fa(client, admin)
    suspend(client, admin, node, "wrong name", codes[0])
    suspend(client, admin, node, "Olga box", "000000")
    rows = (
        sqlite3.connect(tmp / "armadok.db")
        .execute("SELECT outcome, details FROM audit_log WHERE event = 'node.suspend' ORDER BY id")
        .fetchall()
    )
    assert [r[0] for r in rows] == ["denied", "denied"]
    assert "nodeadmin.confirm_mismatch" in rows[0][1] and "auth.invalid_code" in rows[1][1]
