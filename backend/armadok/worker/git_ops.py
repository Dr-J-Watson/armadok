"""Getting a project's files from a Git repository, and keeping them up to date.

A project can start as a copy of a repository's branch (`git.clone`), then be brought to the
newest commit of that branch (`git.pull`) or compared with it (`git.status`). Deploying is a
separate step: this only changes files, like saving a file does.

The design rule: **Git never reads a setting from a place a container can write.** A project
folder is mounted into containers, so anything in it, `.git` included, can be rewritten by one.
Git can be made to run a program or to send its access token elsewhere by a handful of settings
and files (`filter.*`, `core.sshCommand`, `http.proxy`, `commondir`, a submodule's own
configuration...). So the repository data lives in a **private folder of the worker**
(`$ARMADOK_WORKER_DIR/git/<project>`, mode 0700, never mounted), and the project folder holds
the files only (a work tree). The private folder has no remote configured: the address is given
on each command. A `.git` that appears in the project folder is not read.

On top of that:

- only `https://` addresses; every other protocol (`file:`, `ext:`, `ssh:`, `git:`) is turned
  off, redirects are not followed, and the node's own network is off limits unless the
  operator allows it (`ARMADOK_GIT_ALLOW_PRIVATE=true`): the address is resolved first and the
  command is pinned to the address that was checked;
- no shell and no prompt: an argument list built from validated parts, with a deadline, an
  output cap, and an environment built from nothing (no Armadok secret is inherited, and the
  user's or system's Git configuration is ignored);
- the access token travels in the environment of the one command (never in its arguments, the
  address or any repository file) and is scoped to the repository's own host;
- the incoming tree is inspected **before** anything is written: symbolic links, submodules and
  a tracked `.env` (it would replace the private one) are refused;
- submodules are never fetched or read, hooks and file monitors are off, and nothing in the
  repository is ever executed;
- answers carry commit ids and a short subject, never Git's own output (it may quote addresses).
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from armadok.worker import safefs
from armadok.worker.operation import Operation, OperationError
from armadok.worker.projects_ops import PROJECT_NAME, _Project, _root_path

DEADLINE_SECONDS = 25
MAX_OUTPUT_BYTES = 16 * 1024
MAX_TREE_BYTES = 2 * 1024 * 1024  # the listing of a repository's files
MAX_PARALLEL = 3
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# https://host[:port]/path : no user info, no query, no fragment, no space.
URL = re.compile(
    r"https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?"
    r"(/[A-Za-z0-9._~%+@=-]+){1,8}/?"
)
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}")
USERNAME = re.compile(r"[\x21-\x7e]{1,200}")
TOKEN = re.compile(r"[\x21-\x7e]{1,2000}")
ENV_FILE = ".env"
_SAFE_FLAGS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "http.followRedirects=false",
    "-c", "http.proxy=",
    "-c", "http.sslVerify=true",
    "-c", "submodule.recurse=false",
    "-c", "diff.ignoreSubmodules=all",
    "-c", "status.submoduleSummary=false",
    "-c", "core.symlinks=true",
    "-c", "advice.detachedHead=false",
    "-c", "safe.directory=*",
]  # fmt: skip
_lock = threading.Lock()
_busy: set[str] = set()
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


def _check_url(value: Any) -> str:
    """Return the repository address if it is a plain https address, else refuse."""
    if not isinstance(value, str) or len(value) > 500 or not URL.fullmatch(value):
        raise OperationError("worker.invalid_path")
    return value


def _check_branch(value: Any) -> str:
    """Return the branch name if it is a plain one (no `..`, no leading dash), else refuse."""
    if (
        not isinstance(value, str)
        or not BRANCH.fullmatch(value)
        or ".." in value
        or value.endswith((".lock", "/"))
        or "//" in value
    ):
        raise OperationError("worker.invalid_path")
    return value


def _private_dir(project: str) -> Path:
    """Return the worker-private folder that holds a project's repository data."""
    return Path(os.environ.get("ARMADOK_WORKER_DIR", "worker-data")) / "git" / project


def _allow_private() -> bool:
    return os.environ.get("ARMADOK_GIT_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def _pin(url: str) -> list[str]:
    """Resolve the repository's host, refuse the node's own network, and pin the address.

    Without this a repository address could be used to probe the worker's network. The command
    is told which address to use, so a name that answers differently a moment later (DNS
    rebinding) cannot lead it elsewhere.
    """
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    try:
        found = {i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError:
        raise OperationError("worker.git_not_found") from None
    if not found:
        raise OperationError("worker.git_not_found")
    if not _allow_private():
        for address in found:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise OperationError("worker.git_address_not_allowed")
    chosen = sorted(found)[0]
    shown = f"[{chosen}]" if ":" in chosen else chosen
    return ["-c", f"http.curloptResolve={host}:{port}:{shown}"]


def _env(url: str, login: Any, folder_fd: int | None = None, gitdir: Path | None = None):
    """Build the command's environment; the login (if any) becomes a header for that host."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_ASKPASS": "/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    if gitdir is not None:
        # Repository data private to the worker; the project folder is only the work tree.
        env["GIT_DIR"] = str(gitdir)
        env["GIT_WORK_TREE"] = "."
    if login is not None:
        if (
            not isinstance(login, dict)
            or not isinstance(login.get("username"), str)
            or not isinstance(login.get("token"), str)
            or not USERNAME.fullmatch(login["username"])
            or not TOKEN.fullmatch(login["token"])
        ):
            raise OperationError("worker.invalid_path")
        origin = "/".join(url.split("/", 3)[:3]) + "/"
        pair = base64.b64encode(f"{login['username']}:{login['token']}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{origin}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {pair}",
        }
    return env


def _run(
    folder_fd: int, argv: list[str], env: dict[str, str], cap: int = MAX_OUTPUT_BYTES, pin: str = ""
) -> tuple[int, str, bool]:
    """Run git in the folder (by handle); return `(code, output, output_was_cut)`.

    Output is read in small pieces and the process is stopped as soon as it passes the cap, so
    a hostile server cannot make the worker hold a large answer.
    """
    extra = _pin(pin) if pin else []
    with _slots:
        try:
            proc = subprocess.Popen(  # noqa: S603  (argument list, no shell, validated parts)
                ["git", *_SAFE_FLAGS, *extra, *argv],
                cwd=f"/proc/self/fd/{folder_fd}",
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise OperationError("worker.git_missing") from None
        chunks: list[bytes] = []
        state = {"size": 0, "cut": False}

        def reader() -> None:
            assert proc.stdout is not None
            for piece in iter(lambda: proc.stdout.read(4096), b""):  # type: ignore[union-attr]
                if state["size"] < cap:
                    chunks.append(piece[: cap - state["size"]])
                state["size"] += len(piece)
                if state["size"] > cap:
                    state["cut"] = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            code = proc.wait(timeout=DEADLINE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise OperationError("worker.timeout") from None
        finally:
            thread.join(timeout=2)
            if proc.stdout is not None:
                proc.stdout.close()
    return code, b"".join(chunks).decode("utf-8", "replace"), state["cut"]


def _fail(output: str) -> OperationError:
    """Turn Git's complaint into a stable code; the text itself never leaves the node."""
    low = output.lower()
    if any(w in low for w in ("authentication failed", "could not read username", "403", "401")):
        return OperationError("worker.git_auth")
    if "couldn't find remote ref" in low or ("remote branch" in low and "not found" in low):
        return OperationError("worker.git_branch")
    if "not found" in low or "does not exist" in low or "404" in low:
        return OperationError("worker.git_not_found")
    return OperationError("worker.git_failed")


def _git(folder_fd: int, project: str, argv: list[str], url: str, login: Any = None, **kw):
    """Run git against the project's private repository data with its folder as work tree."""
    env = _env(url, login, folder_fd, _private_dir(project))
    return _run(folder_fd, argv, env, **kw)


def _incoming_ok(folder_fd: int, project: str, url: str, ref: str) -> None:
    """Refuse a tree holding links, submodules or a tracked `.env`, before writing any of it.

    A symbolic link in a repository can point at any file of the node and Compose would
    follow it; a submodule is a repository inside the folder; a tracked `.env` would replace
    the private one.
    """
    code, out, cut = _git(
        folder_fd,
        project,
        ["ls-tree", "-r", "-z", "--full-tree", ref],
        url,
        cap=MAX_TREE_BYTES,
    )
    if code != 0 or cut:
        raise OperationError("worker.git_unsafe_tree")
    for entry in filter(None, out.split("\0")):
        meta, _, path = entry.partition("\t")
        mode = meta.split(" ", 1)[0]
        if mode in ("120000", "160000") or path == ENV_FILE or path.startswith(".git/"):
            raise OperationError("worker.git_unsafe_tree")


def _head(folder_fd: int, project: str, url: str) -> tuple[str, str]:
    """Return the checked-out commit id and its subject line."""
    code, out, _ = _git(folder_fd, project, ["log", "-1", "--format=%H%n%s"], url)
    sha, _, subject = out.partition("\n")
    if code != 0 or not SHA.fullmatch(sha.strip()):
        raise OperationError("worker.git_failed")
    return sha.strip(), subject.strip()[:200]


def _dirty(folder_fd: int, project: str, url: str) -> bool:
    """Say whether tracked files were edited by hand (untracked ones, like `.env`, do not count)."""
    code, out, _ = _git(
        folder_fd,
        project,
        ["status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"],
        url,
    )
    if code != 0:
        raise OperationError("worker.git_failed")
    return bool(out.strip())


def _fetch(folder_fd: int, project: str, url: str, branch: str, login: Any) -> None:
    """Fetch the branch's newest commit (depth 1) into `FETCH_HEAD`, then check what it holds."""
    code, out, _ = _git(
        folder_fd,
        project,
        ["fetch", "--depth", "1", "--no-tags", "--no-recurse-submodules", "--", url,
         f"refs/heads/{branch}"],
        url,
        login,
        pin=url,
    )  # fmt: skip
    if code != 0:
        raise _fail(out)
    _incoming_ok(folder_fd, project, url, "FETCH_HEAD")


def _remote_head(folder_fd: int, project: str, url: str, branch: str, login: Any) -> str:
    code, out, _ = _git(
        folder_fd, project, ["ls-remote", "--", url, f"refs/heads/{branch}"], url, login, pin=url
    )
    if code != 0:
        raise _fail(out)
    sha = out.split("\t", 1)[0].strip()
    if not SHA.fullmatch(sha):
        raise OperationError("worker.git_branch")
    return sha


def _claim(project: str) -> None:
    with _lock:
        if project in _busy:
            raise OperationError("worker.project_busy")
        _busy.add(project)


def _release(project: str) -> None:
    with _lock:
        _busy.discard(project)


def _repo_args(args: dict[str, Any]) -> tuple[str, str, str]:
    project = args.get("project")
    if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
        raise OperationError("worker.invalid_path")
    return project, _check_url(args.get("url")), _check_branch(args.get("branch"))


def _has_repo(project: str) -> bool:
    return (_private_dir(project) / "HEAD").is_file()


def git_clone(args: dict[str, Any]) -> dict[str, Any]:
    """Create a new project folder holding the files of a repository's branch."""
    name, url, branch = _repo_args(args)
    login = args.get("login")
    _env(url, login)  # validates the login before anything is created
    _claim(name)
    root_fd = safefs.open_dir(str(_root_path()))
    private = _private_dir(name)
    try:
        try:
            os.mkdir(name, 0o750, dir_fd=root_fd)
        except FileExistsError:
            raise OperationError("worker.conflict") from None
        except OSError:
            raise OperationError("worker.forbidden") from None
        folder = -1
        try:
            folder = safefs.open_dir(name, root_fd)
            # Whatever an earlier project of this name left is stale: start from nothing.
            shutil.rmtree(private, ignore_errors=True)
            private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            code, out, _ = _git(folder, name, ["init", "-q"], url)
            if code != 0:
                raise OperationError("worker.git_failed")
            os.chmod(private, 0o700)
            _fetch(folder, name, url, branch, login)
            code, out, _ = _git(folder, name, ["reset", "--hard", "-q", "FETCH_HEAD"], url)
            if code != 0:
                raise OperationError("worker.git_failed")
            sha, subject = _head(folder, name, url)
        except BaseException:
            if folder >= 0:
                os.close(folder)
                folder = -1
            shutil.rmtree(name, dir_fd=root_fd, ignore_errors=True)
            shutil.rmtree(private, ignore_errors=True)
            raise
        os.close(folder)
    finally:
        os.close(root_fd)
        _release(name)
    return {"commit": sha, "subject": subject}


def git_status(args: dict[str, Any]) -> dict[str, Any]:
    """Return the checked-out commit, whether files were edited by hand, and the remote's tip."""
    project, url, branch = _repo_args(args)
    if not _has_repo(project):
        raise OperationError("worker.git_not_repo")
    _claim(project)
    try:
        with _Project(project) as folder:
            sha, subject = _head(folder.fd, project, url)
            dirty = _dirty(folder.fd, project, url)
            remote = _remote_head(folder.fd, project, url, branch, args.get("login"))
    finally:
        _release(project)
    return {"commit": sha, "subject": subject, "dirty": dirty, "remote": remote}


def git_pull(args: dict[str, Any]) -> dict[str, Any]:
    """Bring the project to the newest commit of its branch; refuse if files were edited by hand."""
    project, url, branch = _repo_args(args)
    if not _has_repo(project):
        raise OperationError("worker.git_not_repo")
    _claim(project)
    try:
        with _Project(project) as folder:
            if _dirty(folder.fd, project, url):
                raise OperationError("worker.git_dirty")
            _fetch(folder.fd, project, url, branch, args.get("login"))
            code, _, _ = _git(folder.fd, project, ["reset", "--hard", "-q", "FETCH_HEAD"], url)
            if code != 0:
                raise OperationError("worker.git_failed")
            sha, subject = _head(folder.fd, project, url)
    finally:
        _release(project)
    return {"commit": sha, "subject": subject}


OPERATIONS: dict[str, Operation] = {
    "git.clone": git_clone,
    "git.status": git_status,
    "git.pull": git_pull,
}
