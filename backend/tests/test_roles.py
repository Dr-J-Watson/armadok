import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.registry import load_registry
from armadok.main import create_app
from armadok.modules.enabled import ENABLED_MODULES
from armadok.modules.roles import builtin

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
    client = TestClient(app)
    client.post(
        "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    admin = login(client, "admin")
    node = client.get("/api/v1/nodes", headers=admin).json()[0]["id"]
    return client, admin, node, tmp_path


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def make_user(client, admin, name, rights=(), scope="*"):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right in rights:
        r = client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
        assert r.status_code == 201, r.text
    return uid, login(client, name)


def role_id(client, admin, name):
    return next(
        r["id"] for r in client.get("/api/v1/roles", headers=admin).json() if r["name"] == name
    )


def assign(client, headers, subject, role, scope="*", subject_type="user", **extra):
    return client.post(
        "/api/v1/assignments",
        headers=headers,
        json={
            "subject_type": subject_type,
            "subject_id": subject,
            "role_id": role,
            "scope": scope,
            **extra,
        },
    )


# --- the built-in roles ---------------------------------------------------------------------


def test_builtin_roles_follow_the_levels_of_the_rights():
    catalogue = load_registry(ENABLED_MODULES).rights()
    roles = builtin.builtin_roles(catalogue)
    assert set(roles) == set(builtin.LEVELS_OF)
    assert "docker.read" in roles["viewer"] and "docker.control" not in roles["viewer"]
    assert {"docker.control", "projects.deploy"} <= set(roles["operator"])
    assert (
        "projects.files.write" in roles["developer"]
        and "traefik.sites.write" not in roles["developer"]
    )
    assert "traefik.sites.write" in roles["editor"] and "docker.control" not in roles["editor"]
    # A role never holds a right without what it depends on.
    assert {"dns.write", "dns.read", "nodes.read"} <= set(roles["domains"])
    for rights in roles.values():
        for right in rights:
            assert set(catalogue[right].depends_on) & set(catalogue) <= set(rights) | {
                d for d in catalogue[right].depends_on if catalogue[d].level is None
            }
    assert "nodes.delete" in roles["owner"] and "nodes.delete" not in roles["administrator"]
    assert "docker.remove" in roles["administrator"] and "docker.remove" not in roles["operator"]
    # Platform administration is never part of a per-node role.
    everything = {r for rights in roles.values() for r in rights}
    assert not {r for r in everything if r.startswith(("users.", "grants.", "apikeys."))}
    assert "audit.read" in roles["auditor"]


def test_builtin_roles_cannot_be_changed_and_custom_ones_can(box):
    client, admin, node, _ = box
    viewer = role_id(client, admin, "viewer")
    assert (
        client.put(
            f"/api/v1/roles/{viewer}", headers=admin, json={"name": "x", "rights": []}
        ).status_code
        == 409
    )
    assert client.delete(f"/api/v1/roles/{viewer}", headers=admin).status_code == 409
    copy = client.post(
        f"/api/v1/roles/{viewer}/duplicate", headers=admin, json={"name": "My viewer"}
    )
    assert copy.status_code == 201
    new = copy.json()["id"]
    changed = client.put(
        f"/api/v1/roles/{new}",
        headers=admin,
        json={"name": "My viewer", "rights": ["nodes.read", "docker.read", "docker.logs"]},
    )
    assert changed.status_code == 200
    listed = {r["name"]: r for r in client.get("/api/v1/roles", headers=admin).json()}
    assert (
        listed["My viewer"]["rights"] == ["docker.logs", "docker.read", "nodes.read"]
        and not listed["My viewer"]["builtin"]
    )
    assert (
        client.post(
            "/api/v1/roles", headers=admin, json={"name": "My viewer", "rights": []}
        ).json()["error"]["code"]
        == "roles.name_taken"
    )
    assert client.delete(f"/api/v1/roles/{new}", headers=admin).status_code == 204


@pytest.mark.parametrize(
    "rights", [["users.read"], ["grants.manage"], ["nope.nothing"], ["apikeys.own"]]
)
def test_a_custom_role_holds_only_per_node_rights(box, rights):
    client, admin, node, _ = box
    r = client.post("/api/v1/roles", headers=admin, json={"name": "bad", "rights": rights})
    assert r.status_code == 422 and r.json()["error"]["code"] == "roles.invalid_rights"


# --- giving roles ---------------------------------------------------------------------------


def test_a_role_on_a_node_gives_its_rights_there_and_only_there(box):
    client, admin, node, _ = box
    bob_id, bob = make_user(client, admin, "bob")
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 404
    made = assign(client, admin, bob_id, role_id(client, admin, "viewer"), scope=f"node:{node}")
    assert made.status_code == 201
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 200
    # Viewing does not allow acting.
    assert (
        client.post(
            f"/api/v1/nodes/{node}/docker/containers/{'a' * 12}/action",
            headers=bob,
            json={"action": "stop"},
        ).status_code
        == 403
    )
    assigned = client.get(
        f"/api/v1/assignments?subject_type=user&subject_id={bob_id}", headers=admin
    ).json()
    assert assigned[0]["role_name"] == "viewer" and assigned[0]["subject_name"] == "bob"
    assert (
        client.delete(f"/api/v1/assignments/{made.json()['id']}", headers=admin).status_code == 204
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 404


def test_groups_hand_their_roles_to_members_and_take_them_back(box):
    client, admin, node, _ = box
    bob_id, bob = make_user(client, admin, "bob")
    group = client.post("/api/v1/groups", headers=admin, json={"name": "Team"}).json()["id"]
    assert (
        assign(
            client,
            admin,
            group,
            role_id(client, admin, "viewer"),
            scope=f"node:{node}",
            subject_type="group",
        ).status_code
        == 201
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 404
    assert (
        client.post(
            f"/api/v1/groups/{group}/members", headers=admin, json={"user_id": bob_id}
        ).status_code
        == 204
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 200
    assert client.get("/api/v1/groups", headers=admin).json()[0]["members"] == [bob_id]
    assert (
        client.delete(f"/api/v1/groups/{group}/members/{bob_id}", headers=admin).status_code == 204
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 404
    client.post(f"/api/v1/groups/{group}/members", headers=admin, json={"user_id": bob_id})
    assert client.delete(f"/api/v1/groups/{group}", headers=admin).status_code == 204
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 404
    assert client.get("/api/v1/assignments", headers=admin).json() == []


def test_an_expired_assignment_gives_nothing(box):
    client, admin, node, tmp = box
    bob_id, bob = make_user(client, admin, "bob")
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    refused = assign(
        client,
        admin,
        bob_id,
        role_id(client, admin, "viewer"),
        scope=f"node:{node}",
        expires_at=past,
    )
    assert refused.status_code == 422
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    ok = assign(
        client,
        admin,
        bob_id,
        role_id(client, admin, "viewer"),
        scope=f"node:{node}",
        expires_at=future,
    )
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 200
    db = sqlite3.connect(tmp / "armadok.db")
    db.execute("UPDATE assignments SET expires_at = ? WHERE id = ?", (past, ok.json()["id"]))
    db.commit()
    assert client.get(f"/api/v1/nodes/{node}", headers=bob).status_code == 404


def test_bad_assignments_are_refused(box):
    client, admin, node, _ = box
    bob_id, _ = make_user(client, admin, "bob")
    viewer = role_id(client, admin, "viewer")
    assert assign(client, admin, bob_id, viewer, scope="node:../x").status_code == 422
    assert assign(client, admin, bob_id, viewer, scope="project:x").status_code == 422
    assert assign(client, admin, bob_id, viewer, scope="node:" + "0" * 32).status_code == 404
    assert assign(client, admin, "nobody", viewer, scope=f"node:{node}").status_code == 404
    assert assign(client, admin, bob_id, "no-role", scope=f"node:{node}").status_code == 404
    assert assign(client, admin, bob_id, viewer, subject_type="robot").status_code == 422


# --- nobody gives more than they hold -------------------------------------------------------


def test_a_delegate_gives_only_what_they_hold_where_they_hold_it(box):
    client, admin, node, _ = box
    other = client.post(
        "/api/v1/nodes", headers=admin, json={"name": "other", "type": "docker"}
    ).json()["id"]
    carol_id, carol = make_user(
        client,
        admin,
        "carol",
        ["grants.manage", "users.read", "nodes.read", "docker.read"],
        scope=f"node:{node}",
    )
    # grants.manage is global in her case only if given globally: give it globally too.
    client.post(
        f"/api/v1/users/{carol_id}/grants",
        headers=admin,
        json={"right": "grants.manage", "scope": "*"},
    )
    client.post(
        f"/api/v1/users/{carol_id}/grants",
        headers=admin,
        json={"right": "users.read", "scope": "*"},
    )
    dave_id, dave = make_user(client, admin, "dave")
    viewer, operator = role_id(client, admin, "viewer"), role_id(client, admin, "operator")
    # she holds nodes.read + docker.read only on `node`, but the viewer role has more.
    assert assign(client, carol, dave_id, viewer, scope=f"node:{node}").status_code == 403
    for right in (
        "projects.read",
        "traefik.read",
        "certs.read",
        "dns.read",
        "workers.read",
        "uptime.read",
        "schedule.read",
        "backups.read",
    ):
        client.post(
            f"/api/v1/users/{carol_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    assert assign(client, carol, dave_id, viewer, scope=f"node:{node}").status_code == 201
    # A machine she cannot see is "not found", not "forbidden".
    assert assign(client, carol, dave_id, viewer, scope=f"node:{other}").status_code == 404
    assert assign(client, carol, dave_id, viewer, scope="*").status_code == 403  # everywhere
    assert (
        assign(client, carol, dave_id, operator, scope=f"node:{node}").status_code == 403
    )  # more rights
    owner = role_id(client, admin, "owner")
    assert assign(client, carol, dave_id, owner, scope=f"node:{node}").status_code == 403


def test_without_the_manage_right_nothing_changes(box):
    client, admin, node, _ = box
    bob_id, bob = make_user(client, admin, "bob", ["users.read"])
    viewer = role_id(client, admin, "viewer")
    assert client.get("/api/v1/roles", headers=bob).status_code == 200
    assert assign(client, bob, bob_id, viewer, scope=f"node:{node}").status_code == 403
    assert client.post("/api/v1/groups", headers=bob, json={"name": "x"}).status_code == 403
    assert (
        client.post("/api/v1/roles", headers=bob, json={"name": "x", "rights": []}).status_code
        == 403
    )
    _, nobody = make_user(client, admin, "eve")
    assert client.get("/api/v1/roles", headers=nobody).status_code == 403


def test_roles_cannot_be_used_to_escalate_through_edits_or_groups(box):
    client, admin, node, _ = box
    carol_id, carol = make_user(client, admin, "carol", ["grants.manage", "users.read"])
    for right in ("nodes.read", "docker.read"):
        client.post(
            f"/api/v1/users/{carol_id}/grants",
            headers=admin,
            json={"right": right, "scope": f"node:{node}"},
        )
    custom = client.post(
        "/api/v1/roles", headers=carol, json={"name": "mine", "rights": ["nodes.read"]}
    ).json()["id"]
    # Once the role is given to someone, adding a right she does not hold is refused: it would
    # reach everyone who has the role.
    bob_id, _ = make_user(client, admin, "bob")
    assign(client, admin, bob_id, custom, scope=f"node:{node}")
    grown = {"name": "mine", "rights": ["nodes.read", "docker.control"]}
    assert client.put(f"/api/v1/roles/{custom}", headers=carol, json=grown).status_code == 403
    # A role she may create, but not give: she does not hold what is in it.
    big = client.post(
        "/api/v1/roles", headers=carol, json={"name": "big", "rights": ["docker.remove"]}
    )
    assert big.status_code == 201
    assert assign(client, carol, bob_id, big.json()["id"], scope=f"node:{node}").status_code == 403
    # A group carrying more than she holds cannot receive members from her.
    group = client.post("/api/v1/groups", headers=admin, json={"name": "ops"}).json()["id"]
    assign(
        client,
        admin,
        group,
        role_id(client, admin, "operator"),
        scope=f"node:{node}",
        subject_type="group",
    )
    dave_id, _ = make_user(client, admin, "dave")
    assert (
        client.post(
            f"/api/v1/groups/{group}/members", headers=carol, json={"user_id": dave_id}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/groups/{group}", headers=carol).status_code == 403
    # ... and she cannot strip the rights of someone above her either.
    every = client.get("/api/v1/assignments", headers=admin).json()
    a = next(x["id"] for x in every if x["role_name"] == "operator")
    assert client.delete(f"/api/v1/assignments/{a}", headers=carol).status_code == 403


def test_a_role_in_use_cannot_be_deleted(box):
    client, admin, node, _ = box
    bob_id, _ = make_user(client, admin, "bob")
    custom = client.post(
        "/api/v1/roles", headers=admin, json={"name": "mine", "rights": ["nodes.read"]}
    ).json()["id"]
    made = assign(client, admin, bob_id, custom, scope=f"node:{node}")
    gone = client.delete(f"/api/v1/roles/{custom}", headers=admin)
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "roles.in_use"
    client.delete(f"/api/v1/assignments/{made.json()['id']}", headers=admin)
    assert client.delete(f"/api/v1/roles/{custom}", headers=admin).status_code == 204


# --- seeing who can what, audit, API keys ---------------------------------------------------


def test_effective_rights_and_node_access_explain_where_rights_come_from(box):
    client, admin, node, _ = box
    bob_id, _ = make_user(client, admin, "bob", ["apikeys.own"])
    group = client.post("/api/v1/groups", headers=admin, json={"name": "Team"}).json()["id"]
    client.post(f"/api/v1/groups/{group}/members", headers=admin, json={"user_id": bob_id})
    assign(
        client,
        admin,
        group,
        role_id(client, admin, "viewer"),
        scope=f"node:{node}",
        subject_type="group",
    )
    got = client.get(f"/api/v1/users/{bob_id}/effective", headers=admin).json()
    assert "apikeys.own" in [g["right"] for g in got["direct"]]
    assert not any(g["right"] in ("docker.read", "nodes.read") for g in got["direct"])
    assert {r["right"] for r in got["from_roles"]} >= {"nodes.read", "docker.read"}
    assert all(
        r["role"] == "viewer" and r["group"] == "Team" and r["scope"] == f"node:{node}"
        for r in got["from_roles"]
    )
    access = {
        a["username"]: a["rights"]
        for a in client.get(f"/api/v1/nodes/{node}/access", headers=admin).json()
    }
    assert "docker.read" in access["bob"] and "docker.control" not in access["bob"]
    assert "docker.control" in access["admin"]
    _, nobody = make_user(client, admin, "eve")
    assert client.get(f"/api/v1/nodes/{node}/access", headers=nobody).status_code == 403


def test_every_change_is_audited_by_name_and_scope(box):
    client, admin, node, tmp = box
    bob_id, _ = make_user(client, admin, "bob")
    group = client.post("/api/v1/groups", headers=admin, json={"name": "Team"}).json()["id"]
    client.post(f"/api/v1/groups/{group}/members", headers=admin, json={"user_id": bob_id})
    a = assign(
        client, admin, bob_id, role_id(client, admin, "viewer"), scope=f"node:{node}"
    ).json()["id"]
    client.delete(f"/api/v1/assignments/{a}", headers=admin)
    events = [
        r[0]
        for r in sqlite3.connect(tmp / "armadok.db").execute(
            "SELECT event FROM audit_log WHERE event LIKE 'group.%'"
            " OR event LIKE 'assignment.%' OR event LIKE 'role.%' ORDER BY id"
        )
    ]
    assert events == ["group.create", "group.member_add", "assignment.add", "assignment.remove"]


def test_an_api_key_can_use_rights_that_come_from_a_role(box):
    client, admin, node, _ = box
    bob_id, bob = make_user(client, admin, "bob", ["apikeys.own"])
    assign(client, admin, bob_id, role_id(client, admin, "viewer"), scope=f"node:{node}")
    key = client.post("/api/v1/apikeys", headers=bob, json={"name": "ci", "rights": ["nodes.read"]})
    assert key.status_code == 201, key.text
    secret = key.json()["key"]
    assert (
        client.get(
            f"/api/v1/nodes/{node}", headers={"Authorization": f"Bearer {secret}"}
        ).status_code
        == 200
    )


# --- findings of the security review ---------------------------------------------------------


def test_emptying_a_role_needs_the_rights_it_takes_away(box):
    client, admin, node, _ = box
    carol_id, carol = make_user(client, admin, "carol", ["grants.manage", "users.read"])
    custom = client.post(
        "/api/v1/roles",
        headers=admin,
        json={"name": "ops", "rights": ["nodes.read", "docker.control", "docker.read"]},
    ).json()["id"]
    assign(client, admin, carol_id, custom, scope=f"node:{node}")  # someone holds it
    bob_id, _ = make_user(client, admin, "bob")
    assign(client, admin, bob_id, custom, scope=f"node:{node}")
    # Carol holds the role's rights on the node only through the role itself... she holds them
    # there, so she may edit; a person who does not hold them may not.
    eve_id, eve = make_user(client, admin, "eve", ["grants.manage", "users.read"])
    stripped = client.put(
        f"/api/v1/roles/{custom}", headers=eve, json={"name": "ops", "rights": []}
    )
    assert stripped.status_code == 403
    still = {r["name"]: r for r in client.get("/api/v1/roles", headers=admin).json()}["ops"]
    assert len(still["rights"]) == 3
    ok = client.put(
        f"/api/v1/roles/{custom}", headers=admin, json={"name": "ops", "rights": ["nodes.read"]}
    )
    assert ok.status_code == 200


def test_the_role_update_audit_names_the_rights_added_and_removed(box):
    client, admin, node, tmp = box
    custom = client.post(
        "/api/v1/roles", headers=admin, json={"name": "ops", "rights": ["nodes.read"]}
    ).json()["id"]
    client.put(
        f"/api/v1/roles/{custom}",
        headers=admin,
        json={"name": "ops", "rights": ["docker.read", "nodes.read"]},
    )
    client.put(
        f"/api/v1/roles/{custom}", headers=admin, json={"name": "ops", "rights": ["docker.read"]}
    )
    import json as _json

    rows = (
        sqlite3.connect(tmp / "armadok.db")
        .execute("SELECT details FROM audit_log WHERE event = 'role.update' ORDER BY id")
        .fetchall()
    )
    details = [_json.loads(r[0]) for r in rows]
    assert details[0]["added"] == ["docker.read"] and details[0]["removed"] == []
    assert details[1]["added"] == [] and details[1]["removed"] == ["nodes.read"]


def test_a_node_the_caller_cannot_see_is_not_found_not_forbidden(box):
    client, admin, node, _ = box
    carol_id, carol = make_user(client, admin, "carol", ["grants.manage", "users.read"])
    bob_id, _ = make_user(client, admin, "bob")
    viewer = role_id(client, admin, "viewer")
    hidden = assign(client, carol, bob_id, viewer, scope=f"node:{node}")
    missing = assign(client, carol, bob_id, viewer, scope="node:" + "0" * 32)
    assert hidden.status_code == missing.status_code == 404


def test_listings_leave_out_machines_the_caller_cannot_see(box):
    client, admin, node, _ = box
    bob_id, _ = make_user(client, admin, "bob")
    assign(client, admin, bob_id, role_id(client, admin, "viewer"), scope=f"node:{node}")
    carol_id, carol = make_user(client, admin, "carol", ["users.read"])
    assert client.get("/api/v1/assignments", headers=admin).json() != []
    assert client.get("/api/v1/assignments", headers=carol).json() == []
    seen = client.get(f"/api/v1/users/{bob_id}/effective", headers=carol).json()
    assert seen["from_roles"] == []
    full = client.get(f"/api/v1/users/{bob_id}/effective", headers=admin).json()
    assert full["from_roles"] != []


def test_custom_roles_cannot_take_a_built_in_name_and_old_ones_step_aside(box):
    client, admin, node, tmp = box
    for name in ("viewer", "Owner", " operator "):
        r = client.post("/api/v1/roles", headers=admin, json={"name": name, "rights": []})
        assert r.status_code == 409, name
    db = sqlite3.connect(tmp / "armadok.db")
    db.execute(
        "INSERT INTO roles (id, name, builtin, created_at) VALUES ('old', 'brandnew', 0, 'x')"
    )
    db.commit()
    from armadok.modules.roles import builtin, repository

    monkey = dict(builtin.LEVELS_OF)
    builtin.LEVELS_OF["brandnew"] = ("view",)
    try:
        db.row_factory = sqlite3.Row
        repository.sync_builtin(db, load_registry(ENABLED_MODULES).rights())
    finally:
        builtin.LEVELS_OF.clear()
        builtin.LEVELS_OF.update(monkey)
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM roles")}
    assert names["old"] == "brandnew (custom)" and names["builtin-brandnew"] == "brandnew"
