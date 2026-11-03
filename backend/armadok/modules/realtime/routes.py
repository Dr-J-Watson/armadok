"""The WebSocket endpoint `/api/v1/realtime`.

Browsers cannot set an `Authorization` header on a WebSocket, and a token in the URL would end
up in logs, so the first message carries it: `{"token": "<access token>"}`. The connection
then only receives; whatever the client sends afterwards is ignored. The session is checked
again every minute, so logging out (or a revoked session) closes the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from armadok.modules.realtime.hub import RealtimeHub, deliver
from armadok.modules.users import repository as users_repo

router = APIRouter(tags=["realtime"])

AUTH_TIMEOUT_SECONDS = 5
RECHECK_SECONDS = 60
MAX_AUTH_BYTES = 2048
MAX_UNAUTHENTICATED = 50
MAX_PER_ADDRESS = 5


def get_hub(app: Any) -> RealtimeHub:
    """Return the app's hub, creating it on first use."""
    if getattr(app.state, "realtime", None) is None:
        app.state.realtime = RealtimeHub()
    return app.state.realtime


def _user_of(app: Any, token: str) -> str | None:
    conn = app.state.settings.open_db()
    try:
        return users_repo.user_for_access_token(conn, token)
    finally:
        conn.close()


def _same_origin(ws: WebSocket) -> bool:
    """Refuse a page from another site (no Origin header = not a browser, which is fine).

    Defence in depth only: there is no cookie to ride on, the token travels in the first
    message. It needs the proxy to pass `Host` (or `X-Forwarded-Host`) unchanged.
    """
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    wanted = origin.split("://", 1)[-1].lower()
    # Behind a proxy the address people typed may arrive as the forwarded host.
    hosts = {ws.headers.get("host", "").lower(), ws.headers.get("x-forwarded-host", "").lower()}
    return wanted in hosts - {""}


@router.websocket("/realtime")
async def realtime(ws: WebSocket) -> None:
    """Receive change signals. Close codes: 4401 not logged in, 4403 wrong origin, 1013 full."""
    if not _same_origin(ws):
        await ws.close(code=4403)
        return
    app = ws.app
    hub = get_hub(app)
    source = ws.client.host if ws.client else ""
    # Not yet logged in: few at a time overall and per address, each for a few seconds at most.
    if hub.waiting_total >= MAX_UNAUTHENTICATED or hub.waiting.get(source, 0) >= MAX_PER_ADDRESS:
        await ws.close(code=1013)
        return
    hub.waiting_total += 1
    hub.waiting[source] = hub.waiting.get(source, 0) + 1
    try:
        await ws.accept()
        try:
            text = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_SECONDS)
            token = json.loads(text)["token"] if len(text) <= MAX_AUTH_BYTES else None
        except (TimeoutError, WebSocketDisconnect, ValueError, KeyError, TypeError):
            token = None
        user_id = await asyncio.to_thread(_user_of, app, token) if isinstance(token, str) else None
    finally:
        hub.waiting_total -= 1
        hub.waiting[source] -= 1
        if hub.waiting[source] <= 0:
            hub.waiting.pop(source, None)
    if user_id is None:
        await ws.close(code=4401)
        return
    client = hub.admit(ws, user_id)
    if client is None:
        await ws.close(code=1013)
        return
    await ws.send_json({"signal": "ready", "node": None})

    async def watch_session() -> None:
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            if await asyncio.to_thread(_user_of, app, token) != user_id:
                await ws.close(code=4401)
                return

    async def drain() -> None:
        # A screen only listens. Reading is how a closed socket is noticed; a client that
        # sends anything at all is cut off, so nobody can keep the server busy with chatter.
        await ws.receive_text()
        await ws.close(code=1008)

    jobs = [
        asyncio.create_task(deliver(app, hub, client)),
        asyncio.create_task(watch_session()),
        asyncio.create_task(drain()),
    ]
    try:
        await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    finally:
        hub.drop(client)
        for job in jobs:
            job.cancel()
            # Nobody awaits these any more: collect their outcome so none is reported as lost.
            job.add_done_callback(lambda done: done.cancelled() or done.exception())
        # Only if the socket is still open (the common end is the client leaving).
        if (
            ws.client_state == WebSocketState.CONNECTED
            and ws.application_state == WebSocketState.CONNECTED
        ):
            with contextlib.suppress(Exception):
                await ws.close()
