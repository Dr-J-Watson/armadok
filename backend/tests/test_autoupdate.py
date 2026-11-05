"""Updating a project by itself: when it redeploys, when it stays quiet, and what stops it."""

import asyncio
import functools
import sqlite3
from types import SimpleNamespace

import pytest

from armadok.core.errors import ApiError
from armadok.modules.autoupdate import repository as state_repo
from armadok.modules.autoupdate import scheduled
from armadok.modules.git import repository as git_repo
from armadok.modules.git import schemas as git_schemas
from armadok.modules.git import service as git_service
from armadok.modules.imageupdates import service as image_service


def sync(test):
    """Run an async test to the end (the suite has no asyncio plugin, like its other tests)."""

    @functools.wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return run


SHA_A, SHA_B = "a" * 40, "b" * 40
D1, D2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
TARGET = {"node_id": "n1", "project": "blog"}


class FakeSettings:
    """Hands out one shared in-memory database, left open by `close`."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY);"
            "INSERT INTO nodes VALUES ('n1');" + git_repo.MIGRATION.sql + state_repo.MIGRATION.sql
        )

    def open_db(self):
        return SimpleNamespace(
            execute=self.conn.execute,
            commit=self.conn.commit,
            close=lambda: None,
        )


@pytest.fixture
def world(monkeypatch):
    """A fake hub: records what was started and what was audited, answers come from `state`."""
    state = SimpleNamespace(
        images=["nginx:1"],
        moved=False,
        newer=False,
        started=[],
        pulled=[],
        pull_error=None,
        deploys=[],
        start_error=None,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(settings=FakeSettings(), registry=SimpleNamespace())
    )

    async def running(app_, node_id, project):
        return state.images

    async def pull(app_, conn, registry, who, node_id, project):
        state.pulled.append(project)
        if state.pull_error:
            raise ApiError(state.pull_error, 409)
        return state.moved

    async def newer(app_, node_id, wanted):
        state.asked = wanted
        return state.newer

    async def start(app_, conn, registry, who, node_id, project, body):
        if state.start_error:
            raise ApiError(state.start_error, 502)
        state.started.append(body)
        return "task"

    monkeypatch.setattr(scheduled.tasks, "recent", lambda *a: state.deploys)
    monkeypatch.setattr(scheduled, "_running_images", running)
    monkeypatch.setattr(scheduled.git_service, "pull_if_behind", pull)
    monkeypatch.setattr(scheduled.image_service, "newer_exists", newer)
    monkeypatch.setattr(scheduled, "start_action", start)
    monkeypatch.setattr(scheduled, "authorise", lambda *a, **k: {})
    return app, state


@sync
async def test_nothing_new_means_nothing_happens(world):
    app, state = world
    await scheduled.run(app, object(), TARGET)
    assert state.pulled == ["blog"]
    assert state.started == []


@sync
async def test_a_newer_image_redeploys_with_a_pull_but_no_rebuild(world):
    app, state = world
    state.newer = True
    await scheduled.run(app, object(), TARGET)
    (body,) = state.started
    assert (body.action, body.pull, body.build) == ("up", True, False)
    assert state.asked == ["nginx:1"]


@sync
async def test_a_new_commit_redeploys_with_a_rebuild_without_asking_the_registries(world):
    app, state = world
    state.moved = True
    await scheduled.run(app, object(), TARGET)
    (body,) = state.started
    assert (body.action, body.pull, body.build) == ("up", True, True)
    assert not hasattr(state, "asked")


@sync
async def test_a_stopped_project_gets_its_files_updated_but_is_never_started(world):
    app, state = world
    state.images, state.moved, state.newer = [], True, True
    await scheduled.run(app, object(), TARGET)
    assert state.pulled == ["blog"]
    assert state.started == []


@sync
async def test_a_refused_pull_stops_before_any_redeploy(world):
    app, state = world
    state.pull_error, state.newer = "worker.git_dirty", True
    with pytest.raises(ApiError) as caught:
        await scheduled.run(app, object(), TARGET)
    assert caught.value.code == "worker.git_dirty"
    assert state.started == []


@sync
async def test_without_the_deploy_right_nothing_is_asked_of_the_machine(world, monkeypatch):
    app, state = world

    def refuse(*args, **kwargs):
        raise ApiError("auth.missing_right", 403)

    monkeypatch.setattr(scheduled, "authorise", refuse)
    with pytest.raises(ApiError):
        await scheduled.run(app, object(), TARGET)
    assert state.pulled == [] and state.started == []


@pytest.mark.parametrize(
    "target",
    [
        {"node_id": "n1"},
        {"node_id": "n1", "project": "../x"},
        {"node_id": "bad id", "project": "blog"},
        {"node_id": "n1", "project": "blog", "extra": 1},
        {"node_id": 3, "project": "blog"},
    ],
)
def test_the_target_must_be_a_machine_and_a_plain_project_name(target):
    with pytest.raises(ValueError):
        scheduled.validate(target)


def test_a_good_target_is_kept_as_it_is():
    assert scheduled.validate(dict(TARGET)) == TARGET


# --- the Git side ------------------------------------------------


@pytest.fixture
def git_world(monkeypatch):
    settings = FakeSettings()
    conn = settings.open_db()
    git_repo.set_source(conn, "n1", "blog", "https://git.example.com/org/blog", "main", "me")
    state = SimpleNamespace(remote=SHA_B, ops=[], audited=[], fail=None)

    async def ask(app, node_id, op, args, model):
        state.ops.append(op)
        if op == "git.status":
            return git_schemas.Status(commit=SHA_A, subject="s", dirty=False, remote=state.remote)
        if state.fail:
            raise ApiError(state.fail, 409)
        return git_schemas.Commit(commit=SHA_B, subject="s")

    monkeypatch.setattr(git_service, "_ask", ask)
    monkeypatch.setattr(git_service, "authorise", lambda *a, **k: {})
    monkeypatch.setattr(
        git_service.audit, "record", lambda *a, **k: state.audited.append(("ok", a[3]))
    )
    monkeypatch.setattr(
        git_service, "audit_failure", lambda *a, **k: state.audited.append(("failed", a[3]))
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(encryption_key=None)))
    registry = SimpleNamespace(audit_events=lambda: ())
    return app, conn, registry, state


@sync
async def test_a_project_behind_its_branch_is_pulled_and_audited(git_world):
    app, conn, registry, state = git_world
    assert await git_service.pull_if_behind(
        app, conn, registry, SimpleNamespace(actor="x"), "n1", "blog"
    )
    assert state.ops == ["git.status", "git.pull"]
    assert state.audited == [("ok", "git.update")]


@sync
async def test_a_project_up_to_date_is_left_alone(git_world):
    app, conn, registry, state = git_world
    state.remote = SHA_A
    assert not await git_service.pull_if_behind(
        app, conn, registry, SimpleNamespace(actor="x"), "n1", "blog"
    )
    assert state.ops == ["git.status"]
    assert state.audited == []


@sync
async def test_a_project_without_a_git_source_is_not_a_git_matter(git_world):
    app, conn, registry, state = git_world
    assert not await git_service.pull_if_behind(
        app, conn, registry, SimpleNamespace(actor="x"), "n1", "other"
    )
    assert state.ops == []


@sync
async def test_a_refused_pull_is_audited_as_a_failure_and_passed_on(git_world):
    app, conn, registry, state = git_world
    state.fail = "worker.git_dirty"
    with pytest.raises(ApiError):
        await git_service.pull_if_behind(
            app, conn, registry, SimpleNamespace(actor="x"), "n1", "blog"
        )
    assert state.audited == [("failed", "git.update")]


# --- the images side ---------------------------------------------


def digests(*refs_and_digests):
    return {
        "images": [
            {"ref": ref, "digests": [digest], "in_use": True} for ref, digest in refs_and_digests
        ]
    }


@pytest.fixture
def image_world(monkeypatch):
    state = SimpleNamespace(listing=digests(("nginx:1", D1)), remote=D2, compared=[])

    async def call(app, registry, node_id, op, args):
        return state.listing

    async def compare(image, allow_private, gate, logins):
        state.compared.append(image.ref)
        return {"state": "update" if state.remote not in image.digests else "current"}

    monkeypatch.setattr(image_service, "call_operation", call)
    monkeypatch.setattr(image_service, "_compare", compare)
    monkeypatch.setattr(image_service, "_open_logins", lambda app, node_id: {})
    app = SimpleNamespace(
        state=SimpleNamespace(registry=None, settings=SimpleNamespace(allow_private_outbound=False))
    )
    return app, state


@sync
async def test_a_newer_version_of_a_project_image_is_seen(image_world):
    app, state = image_world
    assert await image_service.newer_exists(app, "n1", ["nginx:1"])


@sync
async def test_an_image_already_current_is_not_news(image_world):
    app, state = image_world
    state.remote = D1
    assert not await image_service.newer_exists(app, "n1", ["nginx:1"])


@sync
async def test_only_the_images_of_the_project_are_compared(image_world):
    app, state = image_world
    state.listing = digests(("nginx:1", D1), ("redis:7", D1))
    await image_service.newer_exists(app, "n1", ["docker.io/library/nginx:1"])
    assert state.compared == ["nginx:1"]


@sync
async def test_images_that_cannot_be_followed_are_no_news(image_world):
    app, state = image_world
    assert not await image_service.newer_exists(app, "n1", ["sha256:" + "f" * 64, ""])
    assert state.compared == []


# --- interruptions, repeats and creation ---------------------------


@sync
async def test_files_pulled_by_a_run_that_then_failed_are_redeployed_next_time(world):
    app, state = world
    state.moved, state.start_error = True, "worker.unreachable"
    with pytest.raises(ApiError):
        await scheduled.run(app, object(), TARGET)
    state.moved, state.start_error = False, None  # the next run finds the branch already pulled
    await scheduled.run(app, object(), TARGET)
    (body,) = state.started
    assert (body.pull, body.build) == (True, True)
    await scheduled.run(app, object(), TARGET)  # and the debt is paid once
    assert len(state.started) == 1


@sync
async def test_a_redeploy_caused_by_an_image_alone_waits_a_day_before_the_next(world):
    app, state = world
    state.newer = True
    await scheduled.run(app, object(), TARGET)
    await scheduled.run(app, object(), TARGET)
    assert len(state.started) == 1


@sync
async def test_nothing_starts_while_a_deployment_of_the_project_is_running(world):
    app, state = world
    state.newer = True
    state.deploys = [{"state": "running"}]
    await scheduled.run(app, object(), TARGET)
    assert state.pulled == [] and state.started == []


@sync
async def test_a_project_stopped_during_the_checks_is_not_started(world, monkeypatch):
    app, state = world
    state.newer = True
    calls = iter([["nginx:1"], []])

    async def running(app_, node_id, project):
        return next(calls)

    monkeypatch.setattr(scheduled, "_running_images", running)
    await scheduled.run(app, object(), TARGET)
    assert state.started == []


def test_a_second_automatic_update_for_a_project_is_refused_at_creation(monkeypatch):
    settings = FakeSettings()
    conn = settings.open_db()
    settings.conn.executescript(
        "CREATE TABLE scheduled_tasks (id TEXT, node_id TEXT, action TEXT, target TEXT,"
        " created_at TEXT, name TEXT);"
        "INSERT INTO scheduled_tasks VALUES ('t', 'n1', 'project.autoupdate',"
        ' \'{"node_id": "n1", "project": "blog"}\', \'x\', \'x\');'
    )
    monkeypatch.setattr(scheduled, "authorise", lambda *a, **k: {})
    monkeypatch.setattr(
        scheduled.schedule_repo,
        "list_for_node",
        lambda c, n: c.execute("SELECT * FROM scheduled_tasks").fetchall(),
    )
    scheduled.check(conn, None, None, {"node_id": "n1", "project": "api"})
    with pytest.raises(ApiError) as caught:
        scheduled.check(conn, None, None, TARGET)
    assert caught.value.code == "autoupdate.already_scheduled"


def test_creation_needs_the_git_write_right_only_for_a_project_that_follows_git(monkeypatch):
    settings = FakeSettings()
    conn = settings.open_db()
    git_repo.set_source(conn, "n1", "blog", "https://git.example.com/org/blog", "main", "me")
    asked = []
    monkeypatch.setattr(scheduled, "authorise", lambda c, r, w, n, right, obj: asked.append(right))
    monkeypatch.setattr(scheduled.schedule_repo, "list_for_node", lambda c, n: [])
    scheduled.check(conn, None, None, TARGET)
    scheduled.check(conn, None, None, {"node_id": "n1", "project": "api"})
    assert asked == ["projects.deploy", "projects.files.write", "projects.deploy"]
