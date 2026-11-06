"""Endpoints of project terminals: settings, opening one, and the WebSocket that carries it.

A terminal is a shell in a throwaway container that sees one project folder (see
`worker/terminal_ops.py`). Because a shell is as powerful as the container allows, opening one
needs a sensitive right that API keys cannot hold, the project's own switch, and (when the
account has a second factor) a fresh code.

Opening is a normal request, so rights and audit work as everywhere. It returns a one-time
ticket for the WebSocket, which also carries the access token in its first message (as the live
screen does), so a ticket seen in a log is useless alone. What is typed or printed passes
through memory and is never stored, logged or audited.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, field_validator

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.projects import schemas as project_schemas
from armadok.modules.projects import states
from armadok.modules.terminal import repository
from armadok.modules.users import repository as users_repo
from armadok.modules.users import second_factor
from armadok.modules.workers.access import audit_failure, authorise
from armadok.modules.workers.routes import call_operation

router = APIRouter(tags=["terminal"])
_PROJECT = re.compile(project_schemas.PROJECT_PATTERN)
IMAGE_PATTERN = (
    r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*){0,2}"
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
)
TICKET_SECONDS = 30
MAX_PER_USER = 2
MAX_TOTAL = 20
AUTH_TIMEOUT_SECONDS = 5
RECHECK_SECONDS = 60
MAX_MESSAGE = 8192
MAX_INPUT_PER_SECOND = 64 * 1024
MAX_MESSAGES_PER_SECOND = 200
READ_WAIT_SECONDS = 8


@dataclass
class Pending:
    """A terminal that was opened and is waiting for its WebSocket (then, running)."""

    node_id: str
    project: str
    worker_session: str
    user_id: str
    created: float
    connected: bool = False


def _live(app: Any) -> dict[str, Pending]:
    if getattr(app.state, "terminals", None) is None:
        app.state.terminals = {}
    return app.state.terminals


def _project(name: str) -> None:
    if not _PROJECT.fullmatch(name):
        raise ApiError("projects.invalid_name", 422)


class SettingsIn(BaseModel):
    """What a terminal may do, with the fresh second-factor code if the account has one."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool
    writable: bool = False
    network: bool = False
    image: str = Field(default=repository.DEFAULT_IMAGE, pattern=IMAGE_PATTERN, max_length=300)
    code: str = Field(default="", max_length=32)

    @field_validator("image")
    @classmethod
    def no_registry(cls, value: str) -> str:
        """Refuse an image that names a registry: only the default one (or local images) is used."""
        name = value.rsplit(":", 1)[0] if ":" in value.rsplit("/", 1)[-1] else value
        first, _, rest = name.partition("/")
        if rest and ("." in first or first == "localhost"):
            raise ValueError("an image from another registry")
        return value


class OpenIn(BaseModel):
    """Open a terminal of this size."""

    model_config = ConfigDict(extra="forbid")
    cols: int = Field(default=80, ge=10, le=500)
    rows: int = Field(default=24, ge=3, le=200)
    code: str = Field(default="", max_length=32)


def _fresh_code(
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    code: str,
    event: str,
    node_id: str,
) -> None:
    """Demand a fresh second-factor code when the account has one, from the person only."""
    if who.kind != "session":
        raise MissingRight("terminal.not_with_key")
    if not second_factor.is_enabled(conn, who.id):
        return
    try:
        second_factor.require_fresh_code(conn, request.app.state.settings, who.id, code)
    except ApiError as error:
        if error.code != "auth.too_many_attempts":
            audit.record(
                conn,
                registry.audit_events(),
                who.actor,
                event,
                object_type="node",
                object_id=node_id,
                outcome="denied",
                details={"code": error.code},
            )
        raise


@router.get(
    "/nodes/{node_id}/projects/{project}/terminal", summary="Terminal settings of a project"
)
def read_settings(
    node_id: str,
    project: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the project's terminal settings and whether the caller may open one."""
    _project(project)
    authorise(conn, registry, who, node_id, "projects.read", ("project", project))
    states.require_managed(conn, node_id, project)
    scope = f"node:{node_id}/project:{project}"
    return {
        **repository.get(conn, node_id, project),
        "can_manage": who.kind == "session"
        and who.has("projects.terminal", registry.rights(), scope),
    }


@router.put(
    "/nodes/{node_id}/projects/{project}/terminal",
    status_code=204,
    summary="Change terminal settings",
)
def write_settings(
    node_id: str,
    project: str,
    body: SettingsIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Turn the terminal on or off for a project and choose what it may do.

    Needs `projects.terminal` and, with a second factor, a fresh code.
    """
    _project(project)
    authorise(conn, registry, who, node_id, "projects.terminal", ("project", project))
    states.require_managed(conn, node_id, project)
    _fresh_code(request, conn, registry, who, body.code, "terminal.settings", node_id)
    settings = body.model_dump(exclude={"code"})
    repository.save(conn, node_id, project, settings, who.actor)
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "terminal.settings",
        object_type="node",
        object_id=node_id,
        details={"project": project, **settings},
    )


@router.post(
    "/nodes/{node_id}/projects/{project}/terminal", status_code=201, summary="Open a terminal"
)
async def open_terminal(
    node_id: str,
    project: str,
    body: OpenIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Start the project's terminal and return the one-time `ticket` for the WebSocket."""
    _project(project)
    authorise(conn, registry, who, node_id, "projects.terminal", ("project", project))
    states.require_managed(conn, node_id, project)
    settings = repository.get(conn, node_id, project)
    if not settings["enabled"]:
        raise ApiError("terminal.disabled", 409)
    _fresh_code(request, conn, registry, who, body.code, "terminal.open", node_id)
    live = _live(request.app)
    now = time.monotonic()
    for ticket, item in list(live.items()):
        if not item.connected and now - item.created > TICKET_SECONDS:
            live.pop(ticket, None)
            asyncio.create_task(_close_quietly(request.app, registry, item))
    if (
        len(live) >= MAX_TOTAL
        or sum(1 for i in live.values() if i.user_id == who.id) >= MAX_PER_USER
    ):
        raise ApiError("terminal.too_many", 429)
    details: dict[str, Any] = {
        "project": project,
        "image": settings["image"],
        "writable": settings["writable"],
        "network": settings["network"],
    }
    try:
        started = await call_operation(
            request.app,
            registry,
            node_id,
            "terminal.open",
            {
                "project": project,
                "image": settings["image"],
                "writable": settings["writable"],
                "network": settings["network"],
                "cols": body.cols,
                "rows": body.rows,
            },
        )
    except ApiError as error:
        audit_failure(conn, registry, who, "terminal.open", node_id, details, error)
        raise
    session = started.get("session") if isinstance(started, dict) else None
    if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{32}", session):
        raise ApiError("worker.invalid_answer", 502)
    ticket = secrets.token_urlsafe(24)
    pending = Pending(node_id, project, session, who.id, now)
    live[ticket] = pending
    # A terminal nobody connected to is ended after the ticket's lifetime, not left running.
    loop = asyncio.get_running_loop()
    loop.call_later(
        TICKET_SECONDS + 2,
        lambda: asyncio.create_task(_expire(request.app, registry, ticket, pending)),
    )
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        "terminal.open",
        object_type="node",
        object_id=node_id,
        details=details,
    )
    return {"ticket": ticket, "writable": bool(started.get("writable"))}


async def _expire(app: Any, registry: Registry, ticket: str, item: Pending) -> None:
    """End a terminal whose ticket was never used."""
    if not item.connected and _live(app).get(ticket) is item:
        _live(app).pop(ticket, None)
        await _close_quietly(app, registry, item)


async def _close_quietly(app: Any, registry: Registry, item: Pending) -> None:
    """End the node's side of a terminal; a worker that is gone has nothing left to end."""
    try:
        await call_operation(
            app, registry, item.node_id, "terminal.close", {"session": item.worker_session}
        )
    except Exception:  # noqa: BLE001  (best effort: the worker also ends idle terminals itself)
        pass


def _user_of(app: Any, token: str) -> str | None:
    conn = app.state.settings.open_db()
    try:
        return users_repo.user_for_access_token(conn, token)
    finally:
        conn.close()


def _audit_close(app: Any, item: Pending, seconds: int) -> None:
    conn = app.state.settings.open_db()
    try:
        audit.record(
            conn,
            app.state.registry.audit_events(),
            item.user_id,
            "terminal.close",
            object_type="node",
            object_id=item.node_id,
            details={"project": item.project, "seconds": seconds},
        )
    finally:
        conn.close()


@router.websocket("/terminal/{ticket}")
async def terminal_socket(ws: WebSocket, ticket: str) -> None:
    """Carry one terminal.

    First message: `{"token": "<access token>"}`; then `{"in": base64}` and
    `{"resize": [cols, rows]}`. The server sends `{"out": base64}` and `{"closed": true}`.
    Close codes: 4401 not logged in or not the opener, 4404 unknown ticket, 4403 wrong origin.
    """
    from armadok.modules.realtime.routes import _same_origin

    app = ws.app
    registry: Registry = app.state.registry
    if not _same_origin(ws):
        await ws.close(code=4403)
        return
    live = _live(app)
    item = live.get(ticket)
    if item is None or item.connected or time.monotonic() - item.created > TICKET_SECONDS:
        await ws.close(code=4404)
        return
    item.connected = True  # one connection per ticket, taken before anything is awaited
    started = time.monotonic()
    token: Any = None
    try:
        await ws.accept()
        try:
            text = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_SECONDS)
            token = json.loads(text)["token"] if len(text) <= 2048 else None
        except (
            TimeoutError,
            WebSocketDisconnect,
            ValueError,
            KeyError,
            TypeError,
            RecursionError,
        ):
            token = None
        user = await asyncio.to_thread(_user_of, app, token) if isinstance(token, str) else None
        if user is None or user != item.user_id:
            # Someone else (or nobody) knocked: the ticket stays for the person it was made for.
            item.connected = False
            await ws.close(code=4401)
            return
        await _relay(app, registry, ws, ticket, item, token, started)
    except BaseException:
        live.pop(ticket, None)
        await _close_quietly(app, registry, item)
        raise


def _may_still(app: Any, item: Pending) -> bool:
    """Say whether the person still holds the right and the project's terminal is still on."""
    conn = app.state.settings.open_db()
    try:
        registry: Registry = app.state.registry
        who = registry.principal_for(conn, item.user_id)
        scope = f"node:{item.node_id}/project:{item.project}"
        return bool(
            who.has("nodes.read", registry.rights(), f"node:{item.node_id}")
            and who.has("projects.terminal", registry.rights(), scope)
            and repository.get(conn, item.node_id, item.project)["enabled"]
        )
    finally:
        conn.close()


async def _relay(
    app: Any,
    registry: Registry,
    ws: WebSocket,
    ticket: str,
    item: Pending,
    token: str,
    started: float,
) -> None:
    """Carry the terminal's output to the browser and its input back, until either side ends."""
    live = _live(app)

    async def call(op: str, args: dict[str, Any]) -> dict:
        return await call_operation(
            app, registry, item.node_id, op, {"session": item.worker_session, **args}
        )

    async def pump_out() -> None:
        while True:
            answer = await call("terminal.read", {"wait": READ_WAIT_SECONDS})
            data, closed = answer.get("data", ""), answer.get("closed") is True
            if isinstance(data, str) and data:
                await ws.send_text(json.dumps({"out": data, "lost": answer.get("lost") is True}))
            if closed:
                await ws.send_text(json.dumps({"closed": True}))
                return

    async def pump_in() -> None:
        window, sent, count = time.monotonic(), 0, 0
        while True:
            text = await ws.receive_text()
            if len(text) > MAX_MESSAGE:
                return
            now = time.monotonic()
            if now - window >= 1:
                window, sent, count = now, 0, 0
            sent, count = sent + len(text), count + 1
            if sent > MAX_INPUT_PER_SECOND or count > MAX_MESSAGES_PER_SECOND:
                return
            try:
                message = json.loads(text)
            except (ValueError, RecursionError):
                return
            if not isinstance(message, dict):
                return
            if isinstance(message.get("in"), str):
                try:
                    base64.b64decode(message["in"], validate=True)
                except (ValueError, binascii.Error):
                    return
                await call("terminal.write", {"data": message["in"]})
            elif isinstance(message.get("resize"), list) and len(message["resize"]) == 2:
                cols, rows = message["resize"]
                if type(cols) is int and type(rows) is int:
                    await call("terminal.resize", {"cols": cols, "rows": rows})
            else:
                return

    async def watch_session() -> None:
        # The session, the right and the project's own switch are checked again every minute:
        # taking a right away (or turning the terminal off) ends a shell that is already open.
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            if await asyncio.to_thread(_user_of, app, token) != item.user_id:
                return
            if not await asyncio.to_thread(_may_still, app, item):
                return

    jobs = [asyncio.create_task(j()) for j in (pump_out, pump_in, watch_session)]
    try:
        await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for job in jobs:
            job.cancel()
        live.pop(ticket, None)
        # The trace first: it is quick, and it must exist even if closing the node's side
        # is cut short.
        _audit_close(app, item, int(time.monotonic() - started))
        await _close_quietly(app, registry, item)
        try:
            await ws.close()
        except RuntimeError:
            pass
