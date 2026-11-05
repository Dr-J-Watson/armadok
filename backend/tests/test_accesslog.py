"""The access log: the worker reads and reduces it, the hub keeps it a week and masks addresses."""

import asyncio
import json
import logging
import os
import re
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from armadok.core.config import Settings
from armadok.main import create_app
from armadok.modules.traffic import accesslog, routes
from armadok.modules.traffic import accesslog_repository as repo
from armadok.worker import accesslog_ops
from armadok.worker.operation import OperationError
from tests.test_members_admin import PASSWORD, login, owner_with_node, person

NOW = int(time.time())


def line(ip="192.0.2.7", path="/", status=200, **extra):
    body = {
        "ClientHost": ip,
        "RequestHost": "blog.example.com",
        "RequestPath": path,
        "RequestMethod": "GET",
        "DownstreamStatus": status,
        "Duration": 12_000_000,
        "DownstreamContentSize": 512,
        "RouterName": "blog@docker",
        "ServiceName": "blog@docker",
        "StartUTC": "2026-11-05T10:00:00.123456789Z",
    }
    return (json.dumps({**body, **extra}) + "\n").encode()


# --- the worker -------------------------------------------------------


def test_a_line_is_reduced_to_what_the_statistics_need():
    entry = accesslog_ops.entry_of(line(path="/search?token=secret", RequestAddr="x", Cookie="c"))
    assert entry["path"] == "/search"  # the query string never leaves the machine
    assert entry["ip"] == "192.0.2.7" and entry["status"] == 200 and entry["ms"] == 12
    assert set(entry) == {
        "ts", "ip", "host", "path", "method", "status", "ms", "bytes", "router", "service",
    }  # fmt: skip


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1,2]",
        line(ip="not-an-ip"),
        line(status=0),
        line(StartUTC="yesterday"),
        b"x" * 20000,
    ],
)
def test_lines_that_are_not_requests_are_skipped(raw):
    assert accesslog_ops.entry_of(raw.rstrip(b"\n")) is None


def test_control_characters_are_removed_from_text():
    entry = accesslog_ops.entry_of(line(path="/a\x1b[31mb").rstrip(b"\n"))
    assert "\x1b" not in entry["path"]


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    path = tmp_path / "access.log"
    path.write_bytes(b"")
    monkeypatch.setenv("ARMADOK_TRAEFIK_ACCESSLOG", str(path))
    return path


def test_the_first_call_only_notes_the_end_then_new_lines_are_read(logfile):
    logfile.write_bytes(line(path="/old"))
    first = accesslog_ops.read({"inode": None})
    assert first["entries"] == [] and first["offset"] == logfile.stat().st_size
    with logfile.open("ab") as f:
        f.write(line(path="/new"))
    second = accesslog_ops.read({"inode": first["inode"], "offset": first["offset"]})
    assert [e["path"] for e in second["entries"]] == ["/new"]
    third = accesslog_ops.read({"inode": second["inode"], "offset": second["offset"]})
    assert third["entries"] == []


def test_a_half_written_line_waits_for_its_end(logfile):
    first = accesslog_ops.read({"inode": None})
    whole = line(path="/late")
    with logfile.open("ab") as f:
        f.write(whole[:30])
    partial = accesslog_ops.read({"inode": first["inode"], "offset": first["offset"]})
    assert partial["entries"] == [] and partial["offset"] == first["offset"]
    with logfile.open("ab") as f:
        f.write(whole[30:])
    done = accesslog_ops.read({"inode": first["inode"], "offset": partial["offset"]})
    assert [e["path"] for e in done["entries"]] == ["/late"]


def test_a_rotated_or_emptied_log_is_read_from_its_start(logfile):
    logfile.write_bytes(line(path="/a") * 3)
    state = accesslog_ops.read({"inode": None})
    logfile.write_bytes(line(path="/fresh"))  # truncated: smaller than the remembered offset
    again = accesslog_ops.read({"inode": state["inode"], "offset": state["offset"]})
    assert [e["path"] for e in again["entries"]] == ["/fresh"]
    os.replace(logfile, logfile.with_name("old.log"))
    logfile.write_bytes(line(path="/rotated") * 2)  # a new file: another inode
    after = accesslog_ops.read({"inode": again["inode"], "offset": again["offset"]})
    assert [e["path"] for e in after["entries"]] == ["/rotated", "/rotated"]


def test_one_call_returns_a_bounded_number_of_entries_and_loses_none(logfile, monkeypatch):
    monkeypatch.setattr(accesslog_ops, "MAX_ENTRIES", 3)
    state = accesslog_ops.read({"inode": None})
    logfile.write_bytes(b"".join(line(path=f"/p{i}") for i in range(7)))
    seen = []
    for _ in range(4):
        got = accesslog_ops.read({"inode": state["inode"], "offset": state["offset"]})
        seen += [e["path"] for e in got["entries"]]
        state = got
    assert seen == [f"/p{i}" for i in range(7)]


def test_only_the_operator_names_the_file_and_it_must_be_a_regular_file(
    logfile, tmp_path, monkeypatch
):
    # An argument is never a path: this one is ignored and the operator's file is read.
    assert accesslog_ops.read({"inode": None, "path": "/etc/passwd"})["configured"]
    monkeypatch.setenv("ARMADOK_TRAEFIK_ACCESSLOG", str(tmp_path))  # a directory
    with pytest.raises(OperationError) as bad:
        accesslog_ops.read({"inode": None})
    assert bad.value.code == "worker.accesslog_unreadable"
    link = tmp_path / "link.log"
    link.symlink_to(logfile)
    monkeypatch.setenv("ARMADOK_TRAEFIK_ACCESSLOG", str(link))
    with pytest.raises(OperationError):
        accesslog_ops.read({"inode": None})
    monkeypatch.delenv("ARMADOK_TRAEFIK_ACCESSLOG")
    with pytest.raises(OperationError) as unset:
        accesslog_ops.read({"inode": None})
    assert unset.value.code == "worker.accesslog_not_configured"


# --- the hub ----------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "shown"),
    [
        ("192.0.2.77", "192.0.2.0/24"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1::/48"),
        ("::ffff:192.0.2.9", "192.0.2.0/24"),
        ("2002:c000:204::1", "192.0.2.0/24"),  # 6to4: the prefix is the IPv4 address
    ],
)
def test_an_address_is_masked_to_its_network(address, shown):
    assert accesslog.mask(address) == shown


@pytest.fixture
def box(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False, local_node=True))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app


def reader(client, admin, olga, node, name="lena", rights=("traffic.read", "traffic.logs")):
    """A person who holds only these rights on the machine (a role the owner hands out)."""
    made = client.post(
        "/api/v1/roles",
        headers=admin,
        json={"name": f"{name} role", "rights": ["nodes.read", *rights]},
    )
    assert made.status_code == 201, made.text
    uid, headers = person(client, admin, name)
    done = client.post(
        f"/api/v1/nodes/{node}/members",
        headers=olga,
        json={"user_id": uid, "role_id": made.json()["id"]},
    )
    assert done.status_code == 201, done.text
    return headers


def entry(ip, path="/", status=200, ago=60, service="blog@docker"):
    return {
        "ts": NOW - ago, "ip": ip, "host": "blog.example.com", "path": path, "method": "GET",
        "status": status, "ms": 5, "bytes": 100, "router": "blog@docker", "service": service,
    }  # fmt: skip


def keep(app, node, entries, cursor=(1, 10)):
    conn = app.state.settings.open_db()
    try:
        repo.store(conn, node, repo.get_cursor(conn, node), cursor, entries)
        conn.commit()
    finally:
        conn.close()


def test_visitors_are_grouped_and_masked_without_the_ip_right(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    keep(
        app,
        node,
        [
            entry("192.0.2.1", "/a"),
            entry("192.0.2.2", "/a", 404),
            entry("192.0.2.2", "/b"),
            entry("198.51.100.5", "/a"),
        ],
    )
    url = f"/api/v1/nodes/{node}/traffic/visitors?range=24h"
    whole = client.get(url, headers=olga).json()  # the owner holds every right on the machine
    assert whole["masked"] is False
    assert {v["ip"]: v["requests"] for v in whole["visitors"]} == {
        "192.0.2.1": 1, "192.0.2.2": 2, "198.51.100.5": 1,
    }  # fmt: skip
    assert whole["paths"][0] == {"path": "/a", "requests": 3, "errors": 1}
    assert whole["classes"] == {"2xx": 3, "4xx": 1} and whole["reading"] is True
    lena = reader(client, admin, olga, node)
    masked = client.get(url, headers=lena).json()
    assert masked["masked"] is True
    assert {v["ip"]: v["requests"] for v in masked["visitors"]} == {
        "192.0.2.0/24": 3, "198.51.100.0/24": 1,
    }  # fmt: skip
    assert "192.0.2.1" not in json.dumps(masked)


def test_the_log_filters_and_the_address_filter_needs_the_ip_right(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    keep(app, node, [entry("192.0.2.1", "/a_b"), entry("192.0.2.2", "/axb", 500)])
    base = f"/api/v1/nodes/{node}/traffic/log"
    assert len(client.get(base, headers=olga).json()["entries"]) == 2
    only = client.get(base + "?path=a_b", headers=olga).json()["entries"]
    assert [e["path"] for e in only] == ["/a_b"]  # "_" is literal, not a wildcard
    assert [
        e["status"] for e in client.get(base + "?status_class=5", headers=olga).json()["entries"]
    ] == [500]
    assert len(client.get(base + "?ip=192.0.2.1", headers=olga).json()["entries"]) == 1
    lena = reader(client, admin, olga, node)
    assert client.get(base, headers=lena).status_code == 200
    assert client.get(base + "?ip=192.0.2.1", headers=lena).status_code == 403
    assert all("/24" in e["ip"] for e in client.get(base, headers=lena).json()["entries"])


def test_without_the_logs_right_nothing_is_shown_and_an_unseen_machine_is_not_found(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    viewer = reader(client, admin, olga, node, "vic", ("traffic.read",))
    _, stranger = person(client, admin, "sam")
    for path in ("visitors", "log"):
        url = f"/api/v1/nodes/{node}/traffic/{path}"
        assert client.get(url, headers=viewer).status_code == 403
        assert client.get(url, headers=stranger).status_code == 404
    assert client.delete(f"/api/v1/nodes/{node}/traffic/log", headers=viewer).status_code == 403


def test_consulting_is_audited_without_content_and_purging_empties_the_log(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    keep(app, node, [entry("192.0.2.1"), entry("192.0.2.2")])
    client.get(f"/api/v1/nodes/{node}/traffic/visitors", headers=olga)
    assert client.delete(f"/api/v1/nodes/{node}/traffic/log", headers=olga).json() == {"removed": 2}
    events = client.get("/api/v1/audit", headers=admin).json()
    text = json.dumps(events)
    assert "traffic.ip_read" in text and "traffic.purge" in text
    assert "192.0.2.1" not in text
    got = client.get(f"/api/v1/nodes/{node}/traffic/visitors", headers=olga).json()
    assert got["visitors"] == [] and got["kept"] == 0 and got["reading"] is False


def test_old_entries_are_purged_after_a_week_and_the_cap_keeps_the_newest(box, monkeypatch):
    client, admin, app = box
    _, _, node = owner_with_node(client, admin)
    keep(app, node, [entry("192.0.2.1", ago=8 * 86400), entry("192.0.2.2", ago=3600)])
    conn = app.state.settings.open_db()
    try:
        repo.purge(conn)
        assert [r["ip"] for r in conn.execute("SELECT ip FROM access_events")] == ["192.0.2.2"]
        monkeypatch.setattr(repo, "MAX_EVENTS_PER_NODE", 2)
        repo.store(
            conn,
            node,
            repo.get_cursor(conn, node),
            (1, 1),
            [entry(f"192.0.2.{i}", ago=10) for i in range(3, 7)],
        )
        repo.purge(conn)
        assert [r["ip"] for r in conn.execute("SELECT ip FROM access_events ORDER BY id")] == [
            "192.0.2.5", "192.0.2.6",
        ]  # fmt: skip
    finally:
        conn.close()


def test_hostile_chunks_are_cleaned_before_being_kept(box, monkeypatch):
    client, admin, app = box
    _, _, node = owner_with_node(client, admin)
    good = entry("192.0.2.1")
    chunk = {
        "inode": 5,
        "offset": 99,
        "entries": [
            good,
            {**good, "ip": "999.1.1.1"},
            {**good, "path": "x" * 400},
            {**good, "status": 42},
            {**good, "ts": NOW + 10 * 86400},
            {**good, "ts": NOW - 30 * 86400},
            {**good, "host": "a\x00b"},
            "junk",
        ],
    }
    seen = {}

    async def call(app_, registry, node_id, op, args, actor=None):
        seen["args"] = args
        return chunk

    monkeypatch.setattr(accesslog, "call_operation", call)
    assert asyncio.run(accesslog.collect_node(app, node)) is None
    assert seen["args"] == {"inode": None}  # never read: only a baseline is asked for
    conn = app.state.settings.open_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0] == 1
        assert repo.get_cursor(conn, node) == (5, 99)
    finally:
        conn.close()
    assert asyncio.run(accesslog.collect_node(app, node)) is None
    assert seen["args"] == {"inode": 5, "offset": 99}


def test_a_machine_without_an_access_log_reports_the_reason(box, monkeypatch):
    from armadok.core.errors import ApiError

    client, admin, app = box
    _, _, node = owner_with_node(client, admin)

    async def call(*args, **kwargs):
        raise ApiError("worker.accesslog_not_configured", 409)

    monkeypatch.setattr(accesslog, "call_operation", call)
    assert asyncio.run(accesslog.collect_node(app, node)) == "worker.accesslog_not_configured"


def test_routes_module_still_exposes_a_router():
    assert routes.router.prefix == "/nodes/{node_id}/traffic"


def test_non_finite_numbers_in_a_line_skip_the_line_not_the_whole_read():
    bad = line().replace(b'"Duration": 12000000', b'"Duration": NaN')
    assert b"NaN" in bad
    entry = accesslog_ops.entry_of(bad.rstrip(b"\n"))
    assert entry is not None and entry["ms"] == 0
    inf = line().replace(b'"DownstreamContentSize": 512', b'"DownstreamContentSize": Infinity')
    assert accesslog_ops.entry_of(inf.rstrip(b"\n"))["bytes"] == 0


def test_an_address_in_ipv6_dress_is_one_visitor_with_its_ipv4_form():
    entry = accesslog_ops.entry_of(line(ip="::ffff:192.0.2.9").rstrip(b"\n"))
    assert entry["ip"] == "192.0.2.9"


def test_an_inode_too_large_for_the_database_is_refused_not_stored():
    with pytest.raises(ValidationError):
        accesslog.Chunk.model_validate({"inode": 2**63, "offset": 0, "entries": []})


def test_a_chunk_read_before_a_purge_is_thrown_away(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    keep(app, node, [entry("192.0.2.1")], cursor=(1, 10))
    stale = accesslog.Chunk(inode=1, offset=50, entries=[entry("192.0.2.9")])
    client.delete(f"/api/v1/nodes/{node}/traffic/log", headers=olga)  # purge: the position is gone
    assert asyncio.run(asyncio.to_thread(accesslog._store, app, node, (1, 10), stale)) is False
    conn = app.state.settings.open_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0] == 0
        assert repo.get_cursor(conn, node) is None
    finally:
        conn.close()


def test_a_busy_log_is_caught_up_within_one_round(box, monkeypatch):
    client, admin, app = box
    _, _, node = owner_with_node(client, admin)
    monkeypatch.setattr(accesslog, "CHUNK_FULL", 2)
    chunks = [
        {"inode": 5, "offset": 10, "entries": [entry("192.0.2.1"), entry("192.0.2.2")]},
        {"inode": 5, "offset": 20, "entries": [entry("192.0.2.3"), entry("192.0.2.4")]},
        {"inode": 5, "offset": 25, "entries": [entry("192.0.2.5")]},
    ]
    asked = []

    async def call(app_, registry, node_id, op, args, actor=None):
        asked.append(args)
        return chunks[len(asked) - 1]

    monkeypatch.setattr(accesslog, "call_operation", call)
    assert asyncio.run(accesslog.collect_node(app, node)) is None
    assert asked == [{"inode": None}, {"inode": 5, "offset": 10}, {"inode": 5, "offset": 20}]
    conn = app.state.settings.open_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM access_events").fetchone()[0] == 5
    finally:
        conn.close()


def test_a_bad_address_filter_is_refused_and_a_mapped_one_finds_the_ipv4_entries(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    keep(app, node, [entry("192.0.2.1")])
    base = f"/api/v1/nodes/{node}/traffic/log"
    assert client.get(base + "?ip=nonsense", headers=olga).status_code == 422
    found = client.get(base + "?ip=::ffff:192.0.2.1", headers=olga).json()["entries"]
    assert len(found) == 1


def test_the_kept_count_ignores_entries_past_the_retention(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    keep(app, node, [entry("192.0.2.1", ago=9 * 86400), entry("192.0.2.2")])
    got = client.get(f"/api/v1/nodes/{node}/traffic/visitors", headers=olga).json()
    assert got["kept"] == 1


def test_the_cap_is_enforced_when_storing_not_only_hourly(box, monkeypatch):
    client, admin, app = box
    _, _, node = owner_with_node(client, admin)
    monkeypatch.setattr(repo, "MAX_EVENTS_PER_NODE", 3)
    keep(app, node, [entry(f"192.0.2.{i}") for i in range(1, 7)])
    conn = app.state.settings.open_db()
    try:
        assert [r["ip"] for r in conn.execute("SELECT ip FROM access_events ORDER BY id")] == [
            "192.0.2.4", "192.0.2.5", "192.0.2.6",
        ]  # fmt: skip
    finally:
        conn.close()
