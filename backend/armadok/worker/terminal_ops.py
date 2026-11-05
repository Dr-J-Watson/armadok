"""A terminal in a throwaway container that can see one project folder and nothing else.

A real shell cannot be "locked" to a folder without isolation, so the shell is not run on the
machine: it runs in a new container (`docker run --rm -it`) of a small image with

- only the project folder mounted, read-only unless writing was allowed, at `/work`;
- the folder's own owner as user (never root: a root-owned folder is mounted read-only and the
  shell runs as `nobody`), no extra capabilities, no privilege gain, a read-only root file
  system with a small `/tmp`, limited memory, processes and processor;
- no network unless the project's settings allow it.

It ends when the person leaves, after 15 idle minutes, or after an hour. The output is kept in
a small buffer that the hub reads by asking (long waits for new output); what is typed is sent
the same way. Nothing typed or printed is ever logged or returned in an error.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import pty
import re
import secrets
import select
import signal
import stat
import struct
import subprocess
import termios
import threading
import time
from typing import Any

from armadok.worker import compose_ops
from armadok.worker.operation import Operation, OperationError
from armadok.worker.projects_ops import PROJECT_NAME, _root_path

IMAGE = re.compile(
    r"[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*){0,2}"
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
)
SESSION_ID = re.compile(r"[0-9a-f]{32}")
MAX_SESSIONS = 4
MAX_PER_PROJECT = 2
IDLE_SECONDS = 15 * 60
MAX_SECONDS = 60 * 60
MAX_BUFFER = 256 * 1024
MAX_READ = 48 * 1024  # what one read returns (base64 makes it a third larger, under 1 MiB)
MAX_WAIT = 10.0
MAX_INPUT = 4096
FALLBACK_UID = 65534

_lock = threading.Lock()
_sessions: dict[str, _Session] = {}


class _Session:
    """One running terminal: its process, its output buffer, its clock."""

    def __init__(self, project: str, name: str, proc: subprocess.Popen, master: int) -> None:
        self.project, self.name, self.proc, self.master = project, name, proc, master
        self.buffer = bytearray()
        self.lost = False  # output was dropped because nobody read it in time
        self.closed = False
        self.fd_open = True
        # Held while the terminal's descriptor is used or closed: a number that was closed can
        # be handed to something else, and a late write must never land there.
        self.io = threading.Lock()
        self.reader: threading.Thread | None = None
        self.cond = threading.Condition()
        self.started = self.touched = time.monotonic()

    def feed(self, data: bytes) -> None:
        with self.cond:
            self.buffer += data
            if len(self.buffer) > MAX_BUFFER:
                del self.buffer[: len(self.buffer) - MAX_BUFFER]
                self.lost = True
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def _names_a_registry(image: str) -> bool:
    """Say whether the image names a registry (`host.example/org/app`): only the default is used.

    Pulling from a registry chosen by whoever sets the terminal up would make the machine fetch
    from anywhere; images of Docker Hub (or already on the machine) are enough for a shell.
    """
    name = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    first, _, rest = name.partition("/")
    return bool(rest) and ("." in first or first == "localhost")


def _winsize(master: int, cols: int, rows: int) -> None:
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _size(args: dict[str, Any]) -> tuple[int, int]:
    cols, rows = args.get("cols", 80), args.get("rows", 24)
    if (
        type(cols) is not int
        or type(rows) is not int
        or not (10 <= cols <= 500 and 3 <= rows <= 200)
    ):
        raise OperationError("worker.invalid_path")
    return cols, rows


def _session(args: dict[str, Any]) -> _Session:
    sid = args.get("session")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise OperationError("worker.invalid_path")
    with _lock:
        found = _sessions.get(sid)
    if found is None or found.proc is None:
        raise OperationError("worker.not_found")
    return found


def _reader(session: _Session) -> None:
    """Move the terminal's output into the buffer until the process ends."""
    try:
        while not session.closed:
            ready, _, _ = select.select([session.master], [], [], 1.0)
            if not ready:
                continue
            try:
                data = os.read(session.master, 4096)
            except BlockingIOError:
                continue
            if not data:
                break
            session.feed(data)
    except (OSError, ValueError):
        pass
    finally:
        session.finish()


def _end(sid: str) -> None:
    """Stop a terminal and remove its container (idempotent)."""
    with _lock:
        session = _sessions.pop(sid, None)
    if session is None or session.proc is None:
        return
    session.finish()
    try:
        os.killpg(session.proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        subprocess.run(  # noqa: S603  (fixed arguments, the name is ours)
            [compose_ops._docker(), "rm", "-f", session.name],
            env=compose_ops.clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        session.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The reader stops once `closed` is set; only then is the descriptor let go.
    if session.reader is not None:
        session.reader.join(timeout=3)
    with session.io:
        if session.fd_open and not (session.reader and session.reader.is_alive()):
            session.fd_open = False
            try:
                os.close(session.master)
            except OSError:
                pass


def end_stale() -> None:
    """End terminals that have been idle or open too long."""
    now = time.monotonic()
    with _lock:
        stale = [
            sid
            for sid, s in _sessions.items()
            if s.proc is not None
            and (s.closed or now - s.touched > IDLE_SECONDS or now - s.started > MAX_SECONDS)
        ]
    for sid in stale:
        _end(sid)


def _watchdog() -> None:
    """Look for stale terminals every half minute, for as long as the worker runs."""
    while True:
        time.sleep(30)
        end_stale()


_watchdog_started = False


def _ensure_watchdog() -> None:
    global _watchdog_started
    with _lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_watchdog, name="armadok-term-watch", daemon=True).start()


def _host_root() -> str:
    """Return the projects folder as the Docker daemon sees it (`ARMADOK_PROJECTS_HOST_DIR`).

    A worker in a container sees the folder at one path (`/stacks`) and the daemon, which does
    the mounting, at another. Mounting the worker's path would give a different folder, or none,
    so the terminal stays off until the real path is given.
    """
    configured = os.environ.get("ARMADOK_PROJECTS_HOST_DIR", "")
    if (
        not configured.startswith("/")
        or configured == "/"
        or any(c in configured for c in ",:\n\r\0")
        or ".." in configured.split("/")
    ):
        raise OperationError("worker.terminal_not_configured")
    return configured.rstrip("/")


def _folder(project: str) -> tuple[str, int, int]:
    """Return the folder to mount (host path) and the owner of the project's folder.

    The folder is checked here, where the worker sees it (no link, a real folder). The Docker
    daemon resolves the path itself when it mounts it, so this cannot be made airtight against
    someone who can rewrite the projects folder's own entries; that folder is the worker's, and
    no project container is given it.
    """
    host = _host_root()
    path = _root_path() / project
    try:
        info = os.lstat(path)
    except OSError:
        raise OperationError("worker.not_found") from None
    if not stat.S_ISDIR(info.st_mode):
        raise OperationError("worker.not_found")
    return f"{host}/{project}", info.st_uid, info.st_gid


_swept = False


def _worker_tag() -> str:
    """Return a short stable label for this worker (the same after a restart)."""
    folder = os.environ.get("ARMADOK_WORKER_DIR", "worker-data")
    return hashlib.sha256(os.path.abspath(folder).encode()).hexdigest()[:12]


def _sweep_leftovers() -> None:
    """Remove terminal containers an earlier run of this worker left behind (once per start).

    A worker that died took its `docker run` client with it, but not the container, which would
    otherwise live on with nothing to end it. Only containers carrying this worker's own label
    are touched.
    """
    global _swept
    with _lock:
        if _swept:
            return
        _swept = True
    try:
        listed = subprocess.run(  # noqa: S603  (fixed arguments)
            [compose_ops._docker(), "ps", "-aq", "--filter",
             f"label=armadok.terminal.worker={_worker_tag()}"],
            env=compose_ops.clean_env(), stdin=subprocess.DEVNULL, capture_output=True,
            timeout=15, check=False, text=True,
        )  # fmt: skip
        for cid in listed.stdout.split():
            if re.fullmatch(r"[0-9a-f]{12,64}", cid):
                subprocess.run(  # noqa: S603
                    [compose_ops._docker(), "rm", "-f", cid], env=compose_ops.clean_env(),
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
                )  # fmt: skip
    except (subprocess.TimeoutExpired, OSError):
        pass


def terminal_open(args: dict[str, Any]) -> dict[str, Any]:
    """Start a terminal for a project and return its session id.

    Args: `project`, `image`, `writable`, `network` (booleans), `cols`, `rows`.
    """
    project, image = args.get("project"), args.get("image")
    if (
        not isinstance(project, str)
        or not PROJECT_NAME.fullmatch(project)
        or not isinstance(image, str)
        or not IMAGE.fullmatch(image)
        or _names_a_registry(image)
        or type(args.get("writable")) is not bool
        or type(args.get("network")) is not bool
    ):
        raise OperationError("worker.invalid_path")
    cols, rows = _size(args)
    path, uid, gid = _folder(project)
    writable = args["writable"]
    if uid == 0 or gid == 0:
        # A folder owned by root (or by root's group) is never written to from here, and the
        # shell is nobody.
        uid, gid, writable = FALLBACK_UID, FALLBACK_UID, False
    _ensure_watchdog()
    sid, name = secrets.token_hex(16), f"armadok-term-{secrets.token_hex(8)}"
    # The slot is taken before anything starts, so parallel requests cannot all pass the limit.
    with _lock:
        if len(_sessions) >= MAX_SESSIONS:
            raise OperationError("worker.busy")
        if sum(1 for s in _sessions.values() if s.project == project) >= MAX_PER_PROJECT:
            raise OperationError("worker.busy")
        placeholder = _Session(project, name, None, -1)  # type: ignore[arg-type]
        placeholder.closed = True
        _sessions[sid] = placeholder
    _sweep_leftovers()
    mount = f"type=bind,source={path},target=/work,bind-propagation=rprivate" + (
        "" if writable else ",readonly"
    )
    argv = [
        compose_ops._docker(), "run", "--rm", "-it", "--name", name,
        "--label", "armadok.terminal=1", "--label", f"armadok.terminal.worker={_worker_tag()}",
        "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
        "--pids-limit", "128", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
        "--ulimit", "nofile=256:256",
        "--network", "bridge" if args["network"] else "none",
        "--mount", mount, "-w", "/work",
        "-e", "TERM=xterm-256color", "-e", "HOME=/tmp",
        "--stop-timeout", "1", "--entrypoint", "sh", "--", image,
    ]  # fmt: skip
    master, slave = pty.openpty()
    try:
        _winsize(master, cols, rows)
        os.set_blocking(master, False)
        proc = subprocess.Popen(  # noqa: S603  (argument list, no shell, validated parts)
            argv,
            env=compose_ops.clean_env(),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException as error:
        os.close(master)
        with _lock:
            _sessions.pop(sid, None)
        if isinstance(error, OSError):
            raise OperationError("worker.docker_unavailable") from None
        raise
    finally:
        os.close(slave)
    session = _Session(project, name, proc, master)
    session.reader = threading.Thread(target=_reader, args=(session,), daemon=True)
    with _lock:
        _sessions[sid] = session
    session.reader.start()
    return {"session": sid, "writable": writable}


def terminal_read(args: dict[str, Any]) -> dict[str, Any]:
    """Wait up to `wait` seconds for output and return what is there (base64) and if it ended."""
    session = _session(args)
    session.touched = time.monotonic()
    wait = args.get("wait", MAX_WAIT)
    if type(wait) not in (int, float) or not 0 <= wait <= MAX_WAIT:
        raise OperationError("worker.invalid_path")
    deadline = time.monotonic() + wait
    with session.cond:
        while not session.buffer and not session.closed:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            session.cond.wait(left)
        data = bytes(session.buffer[:MAX_READ])
        del session.buffer[:MAX_READ]
        lost, session.lost = session.lost, False
        closed = session.closed and not session.buffer
    if closed:
        _end(args["session"])
    return {"data": base64.b64encode(data).decode(), "lost": lost, "closed": closed}


def terminal_write(args: dict[str, Any]) -> dict[str, Any]:
    """Send what the person typed (base64, at most 4 KiB at a time) to the terminal."""
    session = _session(args)
    try:
        data = base64.b64decode(args.get("data", ""), validate=True)
    except (ValueError, TypeError):
        raise OperationError("worker.invalid_path") from None
    if not data or len(data) > MAX_INPUT:
        raise OperationError("worker.invalid_path")
    session.touched = time.monotonic()
    with session.io:
        if not session.fd_open:
            raise OperationError("worker.not_found")
        try:
            os.write(session.master, data)
        except BlockingIOError:
            raise OperationError("worker.busy") from None  # the shell is not reading
        except OSError:
            raise OperationError("worker.not_found") from None
    return {}


def terminal_resize(args: dict[str, Any]) -> dict[str, Any]:
    """Tell the terminal its window changed size."""
    session = _session(args)
    cols, rows = _size(args)
    with session.io:
        if not session.fd_open:
            raise OperationError("worker.not_found")
        try:
            _winsize(session.master, cols, rows)
        except OSError:
            raise OperationError("worker.not_found") from None
    return {}


def terminal_close(args: dict[str, Any]) -> dict[str, Any]:
    """End the terminal and remove its container."""
    _session(args)
    _end(args["session"])
    return {}


OPERATIONS: dict[str, Operation] = {
    "terminal.open": terminal_open,
    "terminal.read": terminal_read,
    "terminal.write": terminal_write,
    "terminal.resize": terminal_resize,
    "terminal.close": terminal_close,
}
