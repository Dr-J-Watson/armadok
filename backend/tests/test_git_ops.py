"""Git operations of the worker, against a repository on the local disk (no network)."""

import os
import shutil
import subprocess

import pytest

from armadok.worker import git_ops as ops
from armadok.worker.operation import OperationError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "PATH": os.environ["PATH"],
    "HOME": "/tmp",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A repository on disk, reached through a `file://` address that the test allows."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "compose.yaml").write_text("services: {}\n")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "first version")
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(stacks))
    monkeypatch.setenv("ARMADOK_WORKER_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ops, "_pin", lambda url: [])  # a local path has no network to guard
    # Only the test opens the local protocol; the shipped flags allow https alone.
    monkeypatch.setattr(ops, "URL", __import__("re").compile(r"file://.+"))
    pairs = [ops._SAFE_FLAGS[i : i + 2] for i in range(0, len(ops._SAFE_FLAGS), 2)]
    flags = [x for pair in pairs if "protocol" not in pair[1] for x in pair]
    monkeypatch.setattr(ops, "_SAFE_FLAGS", [*flags, "-c", "protocol.file.allow=always"])
    real_env = ops._env
    monkeypatch.setattr(
        ops,
        "_env",
        lambda u, login, *a, **k: {**real_env(u, None, *a, **k), "GIT_ALLOW_PROTOCOL": "file"},
    )
    return origin, stacks, f"file://{origin}"


def code_of(call, *args):
    with pytest.raises(OperationError) as caught:
        call(*args)
    return caught.value.code


def test_clone_status_and_pull_follow_the_branch(remote):
    origin, stacks, url = remote
    done = ops.git_clone({"project": "blog", "url": url, "branch": "main"})
    assert done["subject"] == "first version" and (stacks / "blog" / "compose.yaml").exists()
    args = {"project": "blog", "url": url, "branch": "main"}
    status = ops.git_status(args)
    assert status["dirty"] is False and status["remote"] == status["commit"]
    (origin / "compose.yaml").write_text("services:\n  a: {}\n")
    git(origin, "commit", "-q", "-am", "second version")
    behind = ops.git_status(args)
    assert behind["remote"] != behind["commit"]
    pulled = ops.git_pull(args)
    assert (
        pulled["subject"] == "second version" and ops.git_status(args)["remote"] == pulled["commit"]
    )
    assert "a: {}" in (stacks / "blog" / "compose.yaml").read_text()


def test_hand_edited_tracked_files_stop_a_pull_but_an_env_file_does_not(remote):
    origin, stacks, url = remote
    ops.git_clone({"project": "blog", "url": url, "branch": "main"})
    args = {"project": "blog", "url": url, "branch": "main"}
    (stacks / "blog" / ".env").write_text("SECRET=1\n")
    assert ops.git_status(args)["dirty"] is False
    (stacks / "blog" / "compose.yaml").write_text("edited\n")
    assert ops.git_status(args)["dirty"] is True
    assert code_of(ops.git_pull, args) == "worker.git_dirty"
    assert (stacks / "blog" / "compose.yaml").read_text() == "edited\n"


def test_a_failed_clone_leaves_nothing_and_an_existing_project_is_never_touched(remote):
    origin, stacks, url = remote
    assert code_of(ops.git_clone, {"project": "x", "url": url, "branch": "nope"}) in (
        "worker.git_branch",
        "worker.git_failed",
    )
    assert not (stacks / "x").exists()
    ops.git_clone({"project": "blog", "url": url, "branch": "main"})
    assert (
        code_of(ops.git_clone, {"project": "blog", "url": url, "branch": "main"})
        == "worker.conflict"
    )


@pytest.mark.parametrize(
    "setup",
    [
        # Each plants, inside the project folder, something that would make Git run a program
        # (the marker file) if it were read from there.
        "config-fsmonitor",
        "commondir",
        "gitfile",
        "submodule-filter",
    ],
)
def test_nothing_planted_in_the_project_folder_is_ever_read_by_git(remote, setup):
    origin, stacks, url = remote
    ops.git_clone({"project": "blog", "url": url, "branch": "main"})
    marker = stacks / "pwned"
    work = stacks / "blog"
    evil = f"touch {marker}"
    if setup == "config-fsmonitor":
        (work / ".git").mkdir()
        git(work, "init", "-q")
        git(work, "config", "--local", "core.fsmonitor", evil)
    elif setup == "commondir":
        (work / ".git" / "evil").mkdir(parents=True)
        (work / ".git" / "commondir").write_text("evil\n")
        (work / ".git" / "evil" / "config").write_text(f'[filter "x"]\n\tclean = {evil}\n')
        (work / ".gitattributes").write_text("* filter=x\n")
    elif setup == "gitfile":
        (work / ".git").write_text(f"gitdir: {origin}/.git\n")
    else:
        sub = work / "sub"
        (sub / ".git").mkdir(parents=True)
        git(sub, "init", "-q")
        git(sub, "config", "--local", "filter.x.clean", evil)
        (sub / ".gitattributes").write_text("f filter=x\n")
        (sub / "f").write_text("data")
    args = {"project": "blog", "url": url, "branch": "main"}
    ops.git_status(args)
    ops.git_pull(args)
    assert not marker.exists()


def test_a_repository_with_links_submodules_or_a_tracked_env_is_refused_untouched(remote):
    origin, stacks, url = remote
    for name in ("link", "env", "sub"):
        git(origin, "checkout", "-q", "-B", name, "main")
        if name == "link":
            (origin / "evil").symlink_to("/etc/hostname")
        elif name == "env":
            (origin / ".env").write_text("A=1\n")
        else:
            git(origin, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},vendor")
        git(origin, "add", "-A") if name != "sub" else None
        git(origin, "commit", "-q", "-m", name)
        got = code_of(ops.git_clone, {"project": "p" + name, "url": url, "branch": name})
        assert got == "worker.git_unsafe_tree"
        assert not (stacks / ("p" + name)).exists()
        assert not ops._private_dir("p" + name).exists()
        git(origin, "checkout", "-q", "main")


def test_a_tracked_env_added_upstream_never_replaces_the_private_one(remote):
    origin, stacks, url = remote
    ops.git_clone({"project": "blog", "url": url, "branch": "main"})
    (stacks / "blog" / ".env").write_text("SECRET=mine\n")
    (origin / ".env").write_text("SECRET=theirs\n")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "adds env")
    assert code_of(ops.git_pull, {"project": "blog", "url": url, "branch": "main"}) == (
        "worker.git_unsafe_tree"
    )
    assert (stacks / "blog" / ".env").read_text() == "SECRET=mine\n"


def test_the_machines_own_network_is_off_limits_unless_allowed(monkeypatch):
    monkeypatch.setattr(
        ops.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    assert code_of(ops._pin, "https://git.example.com/a/b") == "worker.git_address_not_allowed"
    monkeypatch.setenv("ARMADOK_GIT_ALLOW_PRIVATE", "true")
    assert ops._pin("https://git.example.com/a/b") == [
        "-c",
        "http.curloptResolve=git.example.com:443:127.0.0.1",
    ]
    monkeypatch.delenv("ARMADOK_GIT_ALLOW_PRIVATE")
    monkeypatch.setattr(
        ops.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    assert ops._pin("https://git.example.com/a/b")[1].endswith(":93.184.216.34")


def test_a_project_that_never_came_from_git_is_not_a_repository(remote):
    origin, stacks, url = remote
    (stacks / "plain").mkdir()
    assert code_of(ops.git_status, {"project": "plain", "url": url, "branch": "main"}) == (
        "worker.git_not_repo"
    )


def test_git_never_inherits_the_workers_environment_and_the_login_is_scoped(monkeypatch):
    monkeypatch.setenv("ARMADOK_ENCRYPTION_KEY", "secret-should-not-leak")
    env = ops._env("https://git.example.com/org/repo", {"username": "bob", "token": "tok" + "en1"})
    assert "ARMADOK_ENCRYPTION_KEY" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "tok" + "en1" not in " ".join(ops._SAFE_FLAGS)


@pytest.mark.parametrize(
    "url",
    ["http://git.example.com/a/b", "file:///etc", "ssh://git@example.com/a/b", "git://example.com/a/b",
     "https://u:p@example.com/a/b", "https://example.com/", "https://example.com", "ext::sh -c id",
     "https://example.com/a b", "https://example.com/a?x=1", "-uhttps://example.com/a/b", 5, None],
)  # fmt: skip
def test_only_plain_https_addresses_are_accepted(url):
    assert code_of(ops._check_url, url) == "worker.invalid_path"


@pytest.mark.parametrize(
    "branch", ["--upload-pack=x", "a..b", "-x", "a b", "a;b", "x.lock", "", 3, None]
)
def test_odd_branch_names_are_refused(branch):
    assert code_of(ops._check_branch, branch) == "worker.invalid_path"
