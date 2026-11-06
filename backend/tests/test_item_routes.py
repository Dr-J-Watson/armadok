"""Item-level Traefik endpoints: switch on and off, read, change, add and remove one item."""

import yaml

from tests.test_middlewares import (  # noqa: F401  (fixtures used by name)
    folder,
    hub,
    sha,
)

# ruff: noqa: F811

FILE = """# my proxy
http:
  routers:
    blog:
      rule: Host(`blog.example.com`)
      service: blog
      middlewares:
        - auth@file
        - lan   # keep
  middlewares:
    auth:
      basicAuth:
        users:
          - "a:b"
    lan:
      ipAllowList:
        sourceRange: ["10.0.0.0/8"]
    both:
      chain:
        middlewares:
          - auth
          - lan
"""


def base(node):
    return f"/api/v1/nodes/{node}/traefik/items"


def put_file(folder, text, name="apps.yml"):
    (folder / name).write_text(text)


def overview(client, admin, node):
    return client.get(f"{base(node)}/overview", headers=admin).json()


def test_switching_a_middleware_off_takes_it_out_of_routers_and_chains_and_back(hub):  # noqa: F811
    client, admin, node, folder = hub
    put_file(folder, FILE)
    body = {"path": "apps.yml", "kind": "middlewares", "name": "lan", "enabled": False}
    done = client.post(f"{base(node)}/toggle", headers=admin, json=body)
    assert done.status_code == 200 and done.json()["changed"] == ["apps.yml"]
    text = (folder / "apps.yml").read_text()
    live = yaml.safe_load(text)["http"]
    assert "lan" not in live["middlewares"]  # Traefik no longer sees it...
    assert live["routers"]["blog"]["middlewares"] == ["auth@file"]  # ...nor any reference to it
    assert live["middlewares"]["both"]["chain"]["middlewares"] == ["auth"]
    flagged = {m["name"]: m["disabled"] for m in overview(client, admin, node)["middlewares"]}
    assert flagged == {"auth": False, "lan": True, "both": False}
    # Asking again changes nothing; switching on gives the file back exactly as it was.
    assert client.post(f"{base(node)}/toggle", headers=admin, json=body).json()["changed"] == []
    on = client.post(f"{base(node)}/toggle", headers=admin, json={**body, "enabled": True})
    assert on.status_code == 200
    assert (folder / "apps.yml").read_text() == FILE


def test_a_one_line_list_naming_the_middleware_is_refused_not_ignored(hub):  # noqa: F811
    client, admin, node, folder = hub
    put_file(folder, FILE.replace("        - auth@file\n        - lan   # keep\n", ""))
    put_file(folder, "http:\n  routers:\n    shop:\n      middlewares: [lan]\n", "shop.yml")
    body = {"path": "apps.yml", "kind": "middlewares", "name": "lan", "enabled": False}
    refused = client.post(f"{base(node)}/toggle", headers=admin, json=body)
    assert (
        refused.status_code == 409 and refused.json()["error"]["code"] == "traefik.flow_reference"
    )
    assert "ipAllowList" in (folder / "apps.yml").read_text().split("lan:")[1][:60]  # untouched


def test_a_router_can_be_switched_off_by_itself(hub):  # noqa: F811
    client, admin, node, folder = hub
    put_file(folder, FILE)
    body = {"path": "apps.yml", "kind": "routers", "name": "blog", "enabled": False}
    assert client.post(f"{base(node)}/toggle", headers=admin, json=body).status_code == 200
    assert "blog" not in (
        yaml.safe_load((folder / "apps.yml").read_text())["http"].get("routers") or {}
    )
    assert overview(client, admin, node)["routers"][0]["disabled"] is True


def test_reading_and_changing_one_item_keeps_the_rest_of_the_file(hub):  # noqa: F811
    client, admin, node, folder = hub
    put_file(folder, FILE)
    params = {"path": "apps.yml", "kind": "middlewares", "name": "lan"}
    got = client.get(base(node), headers=admin, params=params).json()
    assert got["yaml"] == 'ipAllowList:\n  sourceRange: ["10.0.0.0/8"]\n' and got["sha256"] == sha(
        FILE
    )
    new = "ipAllowList:\n  sourceRange:\n    - 192.168.0.0/16\n"
    saved = client.put(
        base(node), headers=admin, json={**params, "yaml": new, "expected_sha256": got["sha256"]}
    )
    assert saved.status_code == 200
    text = (folder / "apps.yml").read_text()
    assert "192.168.0.0/16" in text and "# my proxy" in text and "- lan   # keep" in text
    stale = client.put(
        base(node), headers=admin, json={**params, "yaml": new, "expected_sha256": got["sha256"]}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "worker.conflict"
    bad = client.put(base(node), headers=admin, json={**params, "yaml": "- not a mapping\n"})
    assert bad.status_code == 422


def test_adding_removing_and_the_protection_of_a_middleware_in_use(hub):  # noqa: F811
    client, admin, node, folder = hub
    put_file(folder, FILE)
    made = client.post(
        base(node),
        headers=admin,
        json={"kind": "middlewares", "name": "gzip", "yaml": "compress: {}\n"},
    )
    assert made.status_code == 201 and made.json()["path"] == "middlewares/gzip.yml"
    assert (folder / "middlewares" / "gzip.yml").is_file()
    dup = client.post(
        base(node),
        headers=admin,
        json={"kind": "middlewares", "name": "lan", "yaml": "compress: {}\n", "path": "apps.yml"},
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "traefik.item_exists"
    params = {"path": "apps.yml", "kind": "middlewares", "name": "lan"}
    in_use = client.delete(base(node), headers=admin, params=params)
    assert in_use.status_code == 409 and in_use.json()["error"]["code"] == "traefik.in_use"
    free = client.delete(
        base(node),
        headers=admin,
        params={"path": "middlewares/gzip.yml", "kind": "middlewares", "name": "gzip"},
    )
    assert free.status_code == 204


def test_changes_are_audited_by_name_and_never_by_content(hub):  # noqa: F811
    import sqlite3

    client, admin, node, folder = hub
    put_file(folder, FILE)
    body = {"path": "apps.yml", "kind": "middlewares", "name": "lan", "enabled": False}
    client.post(f"{base(node)}/toggle", headers=admin, json=body)
    db = next(folder.parent.glob("data/armadok.db"))
    rows = (
        sqlite3.connect(db)
        .execute("SELECT event, details FROM audit_log WHERE event LIKE 'traefik.item_%'")
        .fetchall()
    )
    assert rows and rows[0][0] == "traefik.item_toggle" and "10.0.0.0" not in str(rows)


def test_a_form_value_is_written_as_yaml_and_yaml_text_can_be_read_back(hub):
    client, admin, node, folder = hub
    put_file(folder, FILE)
    params = {"path": "apps.yml", "kind": "middlewares", "name": "lan"}
    value = {"ipAllowList": {"sourceRange": ["192.168.0.0/16"]}}
    assert client.put(base(node), headers=admin, json={**params, "value": value}).status_code == 200
    assert "- 192.168.0.0/16" in (folder / "apps.yml").read_text()
    both = client.put(base(node), headers=admin, json={**params, "value": value, "yaml": "a: 1\n"})
    assert both.status_code == 422
    neither = client.put(base(node), headers=admin, json=params)
    assert neither.status_code == 422
    parsed = client.post(
        f"{base(node)}/parse",
        headers=admin,
        json={"yaml": "compress:\n  minResponseBodyBytes: 5\n"},
    )
    assert parsed.json() == {"value": {"compress": {"minResponseBodyBytes": 5}}}
    bad = client.post(f"{base(node)}/parse", headers=admin, json={"yaml": "a: [\n"})
    assert bad.status_code == 422


def test_an_htpasswd_entry_is_a_checkable_bcrypt_hash_and_the_password_is_not_kept(hub):
    import bcrypt

    client, admin, node, folder = hub
    secret = "-".join(["a", "long", "pass"])
    got = client.post(
        f"{base(node)}/htpasswd", headers=admin, json={"username": "alice", "password": secret}
    )
    user, digest = got.json()["entry"].split(":", 1)
    assert user == "alice" and bcrypt.checkpw(secret.encode(), digest.encode())
    short = client.post(
        f"{base(node)}/htpasswd", headers=admin, json={"username": "alice", "password": "short"}
    )
    assert short.status_code == 422


def test_a_mapping_is_written_as_yaml_text(hub):
    client, admin, node, folder = hub
    got = client.post(
        f"{base(node)}/dump",
        headers=admin,
        json={"value": {"compress": {"minResponseBodyBytes": 5}}},
    )
    assert got.json() == {"yaml": "compress:\n  minResponseBodyBytes: 5\n"}
    assert client.post(f"{base(node)}/dump", headers=admin, json={"value": {}}).status_code == 422


def test_adding_a_middleware_never_replaces_one_that_exists(hub):
    client, admin, node, folder = hub
    put_file(folder, FILE)
    (folder / "middlewares").mkdir()
    (folder / "middlewares" / "gzip.yml").write_text(
        "http:\n  middlewares:\n    gzip:\n      compress: {}\n"
    )
    again = client.post(
        base(node),
        headers=admin,
        json={
            "kind": "middlewares",
            "name": "gzip",
            "value": {"stripPrefix": {"prefixes": ["/x"]}},
        },
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "traefik.item_exists"
    assert "compress" in (folder / "middlewares" / "gzip.yml").read_text()
    in_apps = client.post(
        base(node),
        headers=admin,
        json={"kind": "middlewares", "name": "lan", "value": {"compress": {}}},
    )
    assert in_apps.status_code == 409  # the name is taken in another file


def test_a_password_bcrypt_cannot_hold_is_refused_not_a_server_error(hub):
    client, admin, node, folder = hub
    long = "é" * 40  # 40 characters but 80 bytes
    refused = client.post(
        f"{base(node)}/htpasswd", headers=admin, json={"username": "a", "password": long}
    )
    assert refused.status_code == 422
    nul = client.post(
        f"{base(node)}/htpasswd",
        headers=admin,
        json={"username": "a", "password": "abcdefgh\x00ij"},
    )
    assert nul.status_code == 422


def test_a_line_break_trick_in_a_value_cannot_add_a_top_level_key(hub):
    client, admin, node, folder = hub
    put_file(folder, FILE)
    params = {"path": "apps.yml", "kind": "middlewares", "name": "lan"}
    sneaky = client.put(
        base(node), headers=admin, json={**params, "yaml": "x: 1\rtcp:\r  routers: {}\n"}
    )
    assert sneaky.status_code == 422
    assert "tcp" not in (folder / "apps.yml").read_text()
