from datetime import UTC, datetime, timedelta

import pytest

from armadok.core.audit import AUDIT_MIGRATION, UnknownAuditEvent, record
from armadok.core.db import apply_migrations, connect
from armadok.core.errors import MissingRight
from armadok.core.module import Right
from armadok.core.rights import Grant, Principal, can_grant

CATALOGUE = {
    "nodes.read": Right("nodes.read"),
    "nodes.write": Right("nodes.write", depends_on=("nodes.read",)),
}
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_scope_and_dependencies():
    p = Principal("u", (Grant("nodes.write", "node:a"), Grant("nodes.read", "node:a")))
    assert p.has("nodes.write", CATALOGUE, "node:a", NOW)
    assert not p.has("nodes.write", CATALOGUE, "node:b", NOW)


def test_dependency_missing_makes_right_unusable():
    p = Principal("u", (Grant("nodes.write"),))
    assert not p.has("nodes.write", CATALOGUE, now=NOW)
    with pytest.raises(MissingRight):
        p.require("nodes.write", CATALOGUE, now=NOW)


def test_expired_grant_is_ignored():
    p = Principal("u", (Grant("nodes.read", expires_at=NOW - timedelta(seconds=1)),))
    assert not p.has("nodes.read", CATALOGUE, now=NOW)


def test_unknown_right_is_refused():
    assert not Principal("u", (Grant("x"),)).has("x", CATALOGUE, now=NOW)


def test_cannot_grant_more_than_held():
    granter = Principal("g", (Grant("nodes.read", "node:a", NOW + timedelta(days=1)),))

    def ok(grant):
        return can_grant(granter, grant, CATALOGUE, NOW)

    assert ok(Grant("nodes.read", "node:a", NOW + timedelta(hours=1)))
    assert not ok(Grant("nodes.read", "node:b"))
    assert not ok(Grant("nodes.read", "node:a"))
    assert not ok(Grant("nodes.read", "node:a", NOW + timedelta(days=2)))


def test_audit_records_and_rejects_unknown_event(tmp_path):
    conn = connect(tmp_path / "a.db")
    apply_migrations(conn, [AUDIT_MIGRATION])
    record(conn, {"node.create"}, "u1", "node.create", object_type="node", object_id="n1")
    row = conn.execute("SELECT actor, event, outcome FROM audit_log").fetchone()
    assert (row["actor"], row["event"], row["outcome"]) == ("u1", "node.create", "success")
    with pytest.raises(UnknownAuditEvent):
        record(conn, {"node.create"}, "u1", "node.delete")
