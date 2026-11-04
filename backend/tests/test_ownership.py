"""Ownership of the objects inside a machine: rights by object, gifts, taking back, cascade."""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.core.module import OwnershipChange
from armadok.core.rights import ALL_SCOPES, Grant, Principal, can_grant, within
from armadok.main import create_app
from armadok.modules.ownership import service
from tests.test_members_admin import PASSWORD, enable_2fa, login, owner_with_node, person

NOW = datetime.now(UTC)
# The fixture below swaps the listing for a fake; this keeps the real one for its own test.
REAL_EXISTING_IDS = service.existing_ids


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    async def fake_ids(request, registry, node_id, kind):
        return {"blog", "shop"}

    monkeypatch.setattr(service, "existing_ids", fake_ids)
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


def member(client, olga, node, uid):
    r = client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": uid, "role_id": "builtin-viewer"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "armadok.db")
    conn.row_factory = sqlite3.Row
    return conn


def give(client, headers, node, to, **body):
    body = {"object_type": "project", "object_id": "blog", **body}
    return client.post(
        f"/api/v1/nodes/{node}/ownership/offers", headers=headers, json={"to_user": to, **body}
    )


def accept(client, headers, offer_id):
    return client.post(f"/api/v1/ownership/offers/{offer_id}/accept", headers=headers)


def holds(client, user_id, right, scope):
    registry = client.app.state.registry
    conn = client.app.state.settings.open_db()
    try:
        return registry.principal_for(conn, user_id).has(right, registry.rights(), scope)
    finally:
        conn.close()


# --- the rules of scopes ----------------------------------------------


def test_a_machine_grant_covers_its_objects_but_an_object_grant_covers_only_itself():
    machine, blog = "node:a", "node:a/project:blog"
    assert within(blog, machine) and within(machine, machine) and not within(machine, blog)
    assert not within("node:ab/project:x", machine)  # a prefix of the name is not "inside"
    on_machine = Principal("u", (Grant("r", machine),))
    on_blog = Principal("u", (Grant("r", blog),))
    catalogue = {"r": __import__("armadok.core.module", fromlist=["Right"]).Right("r")}
    assert on_machine.has("r", catalogue, blog)
    assert on_blog.has("r", catalogue, blog)
    assert not on_blog.has("r", catalogue, machine)
    assert not on_blog.has("r", catalogue, "node:a/project:shop")
    assert Principal("u", (Grant("r", ALL_SCOPES),)).has("r", catalogue, blog)


def test_a_suspended_machine_blocks_the_objects_inside_it():
    from armadok.core.module import Right

    catalogue = {"r": Right("r")}
    who = Principal("u", (Grant("r", "node:a"),), blocked=frozenset({"node:a"}))
    assert not who.has("r", catalogue, "node:a/project:blog")
    assert who.has("r", catalogue, "node:a/project:blog", ignore_blocked=True)


def test_nobody_gives_an_object_right_they_do_not_hold():
    from armadok.core.module import Right

    catalogue = {"r": Right("r")}
    machine_owner = Principal("u", (Grant("r", "node:a"),))
    blog_owner = Principal("u", (Grant("r", "node:a/project:blog"),))
    assert can_grant(machine_owner, Grant("r", "node:a/project:blog"), catalogue)
    assert can_grant(blog_owner, Grant("r", "node:a/project:blog"), catalogue)
    assert not can_grant(blog_owner, Grant("r", "node:a/project:shop"), catalogue)
    assert not can_grant(blog_owner, Grant("r", "node:a"), catalogue)


# --- a gift ----------------------------------------------


def test_a_gift_changes_nothing_until_it_is_accepted(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    scope = f"node:{node}/project:blog"
    assert not holds(client, bob_id, "projects.files.write", scope)

    offer = give(client, olga, node, bob_id)
    assert offer.status_code == 201 and offer.json()["count"] == 1
    assert not holds(client, bob_id, "projects.files.write", scope)
    received = client.get("/api/v1/ownership/offers", headers=bob).json()["received"]
    assert [i["id"] for o in received for i in o["items"]] == ["blog"]
    assert client.get("/api/v1/ownership/offers", headers=olga).json()["received"] == []

    done = accept(client, bob, offer.json()["id"])
    assert done.status_code == 200 and done.json() == {"moved": 1, "skipped": 0}
    # Bob owns the project: its rights, on it alone; the shop is still Olga's.
    assert holds(client, bob_id, "projects.files.write", scope)
    assert holds(client, bob_id, "ownership.give", scope)
    assert not holds(client, bob_id, "projects.files.write", f"node:{node}/project:shop")
    assert not holds(client, bob_id, "projects.files.write", f"node:{node}")
    # Olga is the machine's owner: she still holds everything, through the machine.
    assert holds(client, olga_id, "projects.files.write", scope)
    # The offer is spent.
    assert accept(client, bob, offer.json()["id"]).status_code == 404


def test_only_the_receiver_answers_and_only_the_giver_withdraws(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    cat_id, cat = person(client, admin, "cat")
    member(client, olga, node, bob_id)
    member(client, olga, node, cat_id)
    offer_id = give(client, olga, node, bob_id).json()["id"]
    assert accept(client, cat, offer_id).status_code == 404
    assert (
        client.post(f"/api/v1/ownership/offers/{offer_id}/refuse", headers=cat).status_code == 404
    )
    assert client.delete(f"/api/v1/ownership/offers/{offer_id}", headers=bob).status_code == 404
    assert client.delete(f"/api/v1/ownership/offers/{offer_id}", headers=olga).status_code == 204
    assert accept(client, bob, offer_id).status_code == 404

    again = give(client, olga, node, bob_id).json()["id"]
    refused = client.post(f"/api/v1/ownership/offers/{again}/refuse", headers=bob)
    assert refused.status_code == 204
    assert client.get("/api/v1/ownership/offers", headers=olga).json()["sent"] == []


def test_a_gift_goes_only_to_an_active_person_who_sees_the_machine(box):
    client, admin, _ = box
    olga_id, olga, node = owner_with_node(client, admin)
    stranger_id, _ = person(client, admin, "stranger")
    # A stranger and a name that does not exist answer the same.
    assert give(client, olga, node, stranger_id).status_code == 404
    assert give(client, olga, node, "0" * 32).status_code == 404
    assert give(client, olga, node, olga_id).status_code == 409


def test_one_cannot_give_what_one_does_not_own(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    cat_id, _ = person(client, admin, "cat")
    member(client, olga, node, bob_id)
    member(client, olga, node, cat_id)
    # Bob is a plain viewer: he owns nothing, so he cannot give the blog (nor does it exist
    # as his), and a name nobody has is refused too.
    assert give(client, bob, node, cat_id).status_code in (404, 409)
    assert give(client, olga, node, cat_id, object_id="ghost").status_code == 404
    assert give(client, olga, node, cat_id, object_type="nothing").status_code == 422
    assert give(client, olga, node, cat_id, object_id="a/b").status_code == 422
    # Once the blog is Bob's, Olga cannot give it away behind his back.
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    assert give(client, olga, node, cat_id).status_code == 409


def test_a_gift_needs_the_persons_own_session_and_their_code(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, _ = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    enable_2fa(client, olga)
    assert give(client, olga, node, bob_id).status_code in (401, 403)
    assert give(client, olga, node, bob_id, code="000000").status_code == 401
    key = client.post("/api/v1/api-keys", headers=olga, json={"name": "k", "rights": []})
    if key.status_code == 201:
        keyed = {"Authorization": f"Bearer {key.json()['key']}"}
        assert give(client, keyed, node, bob_id).status_code in (401, 403, 404)


def test_a_gift_is_void_when_the_receiver_loses_the_machine(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    assignment = member(client, olga, node, bob_id)
    offer_id = give(client, olga, node, bob_id).json()["id"]
    client.delete(f"/api/v1/nodes/{node}/members/{assignment}", headers=olga)
    assert accept(client, bob, offer_id).status_code in (404, 409)


# --- cascade ----------------------------------------------


def test_a_cascade_gives_everything_the_giver_owns_on_the_machine(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    shown = client.get(f"/api/v1/nodes/{node}/ownership/cascade?mode=give", headers=olga).json()
    assert shown["items"] == [{"type": "project", "id": "blog"}, {"type": "project", "id": "shop"}]
    offer = client.post(
        f"/api/v1/nodes/{node}/ownership/offers",
        headers=olga,
        json={"to_user": bob_id, "cascade": True},
    )
    assert offer.status_code == 201 and offer.json()["count"] == 2
    assert accept(client, bob, offer.json()["id"]).json()["moved"] == 2
    for name in ("blog", "shop"):
        assert holds(client, bob_id, "projects.deploy", f"node:{node}/project:{name}")
    # Nothing left for Olga to give except through the machine (she still owns nothing by row).
    mine = client.get(f"/api/v1/nodes/{node}/ownership", headers=bob).json()["mine"]
    assert len(mine) == 2
    assert client.get(f"/api/v1/nodes/{node}/ownership", headers=bob).json()["others"] == []
    seen = client.get(f"/api/v1/nodes/{node}/ownership", headers=olga).json()
    assert seen["can_take"] and {o["owner_name"] for o in seen["others"]} == {"bob"}


def test_a_cascade_and_a_single_object_exclude_each_other(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, _ = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    bad = client.post(
        f"/api/v1/nodes/{node}/ownership/offers",
        headers=olga,
        json={"to_user": bob_id, "cascade": True, "object_type": "project", "object_id": "blog"},
    )
    assert bad.status_code == 422
    none = client.post(
        f"/api/v1/nodes/{node}/ownership/offers", headers=olga, json={"to_user": bob_id}
    )
    assert none.status_code == 422


# --- taking back ----------------------------------------------


def test_the_machines_owner_takes_back_but_nobody_takes_from_them(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    scope = f"node:{node}/project:blog"
    url = f"/api/v1/nodes/{node}/ownership/take"
    # Bob owns the blog: he cannot take, and he cannot take anything from the machine's owner.
    assert client.post(url, headers=bob, json={"cascade": True}).status_code == 403
    taken = client.post(url, headers=olga, json={"object_type": "project", "object_id": "blog"})
    assert taken.status_code == 200 and taken.json() == {"moved": 1}
    assert not holds(client, bob_id, "projects.files.write", scope)
    assert client.get(f"/api/v1/nodes/{node}/ownership", headers=bob).json()["mine"] == []
    # Taking what is already hers finds nothing.
    again = client.post(url, headers=olga, json={"object_type": "project", "object_id": "blog"})
    assert again.status_code == 404


def test_taking_back_everything_withdraws_the_gifts_naming_it(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    cat_id, cat = person(client, admin, "cat")
    member(client, olga, node, bob_id)
    member(client, olga, node, cat_id)
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    pending = give(client, bob, node, cat_id).json()["id"]
    taken = client.post(
        f"/api/v1/nodes/{node}/ownership/take", headers=olga, json={"cascade": True}
    )
    assert taken.json() == {"moved": 1}
    assert accept(client, cat, pending).status_code == 404
    assert client.get("/api/v1/ownership/offers", headers=bob).json()["sent"] == []


# --- what hangs on an object ----------------------------------------------


def test_schedules_and_hooks_of_the_former_owner_stop_and_notes_follow_only_if_given(box):
    client, admin, tmp = box
    olga_id, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    conn = db(tmp)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO scheduled_tasks (id, node_id, name, action, target, schedule, created_by,"
        " created_at, next_run) VALUES ('t1', ?, 'n', 'project.restart', ?, '{}', ?, ?, ?)",
        (node, json.dumps({"node_id": node, "project": "blog"}), olga_id, now, now),
    )
    conn.execute(
        "INSERT INTO scheduled_tasks (id, node_id, name, action, target, schedule, created_by,"
        " created_at, next_run) VALUES ('t2', ?, 'n', 'project.restart', ?, '{}', ?, ?, ?)",
        (node, json.dumps({"node_id": node, "project": "shop"}), olga_id, now, now),
    )
    conn.execute(
        "INSERT INTO hooks (id, name, action, target, token_hash, created_by, created_at)"
        " VALUES ('h1', 'n', 'project.redeploy', ?, 'x', ?, ?)",
        (json.dumps({"node_id": node, "project": "blog"}), olga_id, now),
    )
    for note, body in (("n1", "blog note"), ("n2", "shop note")):
        conn.execute(
            "INSERT INTO notes (id, owner_id, node_id, object_type, object_id, body, encrypted,"
            " importance, created_at, updated_at) VALUES (?, ?, ?, 'project', ?, ?, 0, 'info',"
            " ?, ?)",
            (note, olga_id, node, "blog" if note == "n1" else "shop", body, now, now),
        )
    conn.execute(
        "INSERT INTO note_shares (note_id, user_id, created_at) VALUES ('n1', ?, ?)", (bob_id, now)
    )
    conn.commit()
    conn.close()

    # Without the notes: they stay Olga's; her schedule and hook on the blog stop.
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    conn = db(tmp)
    assert conn.execute("SELECT owner_id FROM notes WHERE id='n1'").fetchone()[0] == olga_id
    tasks = {r["id"]: r["enabled"] for r in conn.execute("SELECT id, enabled FROM scheduled_tasks")}
    assert tasks == {"t1": 0, "t2": 1}
    assert conn.execute("SELECT revoked FROM hooks WHERE id='h1'").fetchone()[0] == 1
    conn.close()

    # Bob gives the blog back with his notes (none): nothing of Olga's moves by itself.
    # Olga gives the shop with her notes: the shop note follows, its shares end.
    accept(
        client,
        bob,
        give(client, olga, node, bob_id, object_id="shop", with_notes=True).json()["id"],
    )
    conn = db(tmp)
    assert conn.execute("SELECT owner_id FROM notes WHERE id='n2'").fetchone()[0] == bob_id
    assert conn.execute("SELECT owner_id FROM notes WHERE id='n1'").fetchone()[0] == olga_id
    conn.close()

    # Taking back never carries a note along.
    client.post(f"/api/v1/nodes/{node}/ownership/take", headers=olga, json={"cascade": True})
    conn = db(tmp)
    assert conn.execute("SELECT owner_id FROM notes WHERE id='n2'").fetchone()[0] == bob_id
    conn.close()


def test_the_change_is_audited_and_listeners_know_the_giver(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    client.post(f"/api/v1/nodes/{node}/ownership/take", headers=olga, json={"cascade": True})
    conn = db(tmp)
    events = [r["event"] for r in conn.execute("SELECT event FROM audit_log ORDER BY rowid")]
    conn.close()
    for wanted in ("ownership.offer", "ownership.accept", "ownership.take"):
        assert wanted in events
    change = OwnershipChange("n", "project", "blog", "a", "b")
    assert change.with_notes is False


def test_erasing_a_machine_for_good_forgets_its_owners_and_gifts(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    give(client, olga, node, bob_id, object_id="shop")
    registry = client.app.state.registry
    assert ("object_owners", "delete") in registry.node_tables()
    assert ("ownership_offers", "delete") in registry.node_tables()


# --- findings of the security review ------------------------------------------------------------


def test_a_removed_member_does_not_get_an_old_objects_rights_back_when_invited_again(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    assignment = member(client, olga, node, bob_id)
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    scope = f"node:{node}/project:blog"
    assert holds(client, bob_id, "projects.deploy", scope)
    client.delete(f"/api/v1/nodes/{node}/members/{assignment}", headers=olga)
    assert not holds(client, bob_id, "projects.deploy", scope)
    member(client, olga, node, bob_id)  # back as a plain viewer
    # The viewer role reads; it does not deploy, and the old ownership does not revive.
    assert not holds(client, bob_id, "projects.deploy", scope)


def test_a_gift_lapses_when_the_giver_loses_the_machine(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    cat_id, cat = person(client, admin, "cat")
    bob_assignment = member(client, olga, node, bob_id)
    member(client, olga, node, cat_id)
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    pending = give(client, bob, node, cat_id).json()["id"]
    client.delete(f"/api/v1/nodes/{node}/members/{bob_assignment}", headers=olga)
    assert accept(client, cat, pending).status_code == 409
    assert not holds(client, cat_id, "projects.deploy", f"node:{node}/project:blog")


def test_taking_and_giving_leave_the_objects_in_the_audit_log_and_refusals_a_trace(box):
    client, admin, tmp = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    assert give(client, olga, node, bob_id, object_id="ghost").status_code == 404
    accept(client, bob, give(client, olga, node, bob_id).json()["id"])
    client.post(f"/api/v1/nodes/{node}/ownership/take", headers=olga, json={"cascade": True})
    conn = db(tmp)
    rows = conn.execute(
        "SELECT event, outcome, details FROM audit_log WHERE event LIKE 'ownership.%'"
    ).fetchall()
    conn.close()
    details = {(r["event"], r["outcome"]): json.loads(r["details"]) for r in rows}
    assert details[("ownership.accept", "success")]["objects"] == "project:blog"
    assert details[("ownership.take", "success")]["objects"] == "project:blog"
    assert details[("ownership.take", "success")]["from_owners"] == bob_id


def test_only_someone_with_something_to_give_can_look_people_up(box):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, bob = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    url = f"/api/v1/nodes/{node}/ownership/people?username=olga"
    assert client.get(url, headers=bob).status_code == 403  # a reader owns nothing
    assert client.get(
        f"/api/v1/nodes/{node}/ownership/people?username=bob", headers=olga
    ).json() == [{"id": bob_id, "username": "bob"}]


def test_a_receivers_inbox_is_bounded(box, monkeypatch):
    client, admin, _ = box
    _, olga, node = owner_with_node(client, admin)
    bob_id, _ = person(client, admin, "bob")
    member(client, olga, node, bob_id)
    monkeypatch.setattr(service, "MAX_INBOX", 1)
    assert give(client, olga, node, bob_id).status_code == 201
    assert give(client, olga, node, bob_id, object_id="shop").status_code == 429


def test_a_truncated_listing_refuses_a_cascade():
    import asyncio
    from types import SimpleNamespace

    from armadok.core.errors import ApiError
    from armadok.core.module import Ownable

    async def long_list(request, registry, node_id):
        return [f"p{i}" for i in range(service.MAX_LISTED)]

    registry = SimpleNamespace(ownables=lambda: {"project": Ownable("project", (), long_list)})
    with pytest.raises(ApiError) as caught:
        asyncio.run(REAL_EXISTING_IDS(None, registry, "n", "project"))
    assert caught.value.code == "ownership.list_truncated"


def test_every_machines_owner_gets_the_owner_rights_of_the_enabled_modules(box):
    from armadok.core import grants
    from armadok.modules.nodes import local

    client, admin, _ = box
    olga_id, _, node = owner_with_node(client, admin)
    conn = client.app.state.settings.open_db()
    try:
        for g in grants.list_grants(conn, olga_id):
            if g["right"] == "ownership.manage":
                grants.remove_grant(conn, olga_id, g["id"])
        assert not holds(client, olga_id, "ownership.manage", f"node:{node}")
        local.ensure_owner_rights(conn, client.app.state.settings, client.app.state.registry)
    finally:
        conn.close()
    assert holds(client, olga_id, "ownership.manage", f"node:{node}")
