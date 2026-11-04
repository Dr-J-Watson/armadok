"""Endpoints of volume backups, per node.

Reading needs `backups.read`, starting a backup `backups.create`. Restoring over a volume and
deleting an archive need `backups.manage` (sensitive). Archives never leave the machine: the
hub lists them, names them and asks the worker to delete or restore them.
"""

from __future__ import annotations

import base64
import sqlite3
import time

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.backups import repository as repo
from armadok.modules.backups import service, tickets
from armadok.modules.users import repository as users_repo
from armadok.modules.users import second_factor
from armadok.modules.workers.access import authorise

router = APIRouter(prefix="/nodes/{node_id}/backups", tags=["backups"])

Volume = Path(pattern=service.VOLUME.pattern)
ArchiveName = Path(pattern=service.ARCHIVE.pattern)


class BackupIn(BaseModel):
    """How many archives of this volume to keep after the backup."""

    model_config = ConfigDict(extra="forbid")
    keep: int = Field(default=service.DEFAULT_KEEP, ge=1, le=365)


class RestoreIn(BaseModel):
    """What to restore and where; the volume name is typed again to confirm."""

    model_config = ConfigDict(extra="forbid")
    archive: str = Field(pattern=service.ARCHIVE.pattern)
    volume: str = Field(pattern=service.VOLUME.pattern)
    confirm: str = Field(max_length=128)
    # Needed to restore into a volume other than the one the archive came from.
    other: bool = False
    # Empty the volume first, so it ends up exactly as the archive was (not a mix of both).
    clear: bool = False


class DownloadIn(BaseModel):
    """A fresh second-factor code, to be given a one-time download address."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=30)


def _actor(request: Request) -> str | None:
    """Return the account behind a person's own session (emergency access is for sessions only)."""
    if getattr(request.state, "actor_kind", "") == "session":
        return getattr(request.state, "actor_id", None)
    return None


def _audit(conn, registry, who, event, node_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        details=details,
    )


MAX_DOWNLOADS_PER_NODE = 3  # streams at once: each piece is a call to the machine's worker
MAX_DOWNLOAD_BYTES = 2**40  # 1 TiB
MAX_DOWNLOAD_SECONDS = 6 * 3600
RECHECK_EVERY = 64  # pieces (24 MiB): the right is looked at again this often during a download


def _still_allowed(app, user_id: str, node_id: str) -> bool:
    """Tell whether the person may still download: active, not blocked, right still held."""
    conn = app.state.settings.open_db()
    try:
        registry = app.state.registry
        person = users_repo.get_user(conn, user_id)
        if person is None or not person["is_active"] or registry.account_blocked(conn, user_id):
            return False
        who = registry.principal_for(conn, user_id)
        return who.has("backups.download", registry.rights(), f"node:{node_id}")
    finally:
        conn.close()


@router.get("", summary="Archives and recent jobs of a node")
async def overview(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the archives in the machine's backup folder and the latest jobs.

    `configured` is False when the worker has no backup folder: nothing else is then offered.
    """
    authorise(conn, registry, who, node_id, "backups.read")
    scope, catalogue = f"node:{node_id}", registry.rights()
    try:
        answer = await service.ask(
            request.app, node_id, "backups.list", {}, service.Archives, _actor(request)
        )
        archives, truncated, configured = answer.archives, answer.truncated, True
    except ApiError as error:
        if error.code != "worker.backups_not_configured":
            raise
        archives, truncated, configured = [], False, False
    return {
        "configured": configured,
        "archives": [a.model_dump() for a in archives],
        "truncated": truncated,
        "jobs": repo.recent(conn, node_id),
        "can_create": who.has("backups.create", catalogue, scope),
        "can_manage": who.has("backups.manage", catalogue, scope),
        "can_download": who.has("backups.download", catalogue, scope) and who.kind == "session",
        "default_keep": service.DEFAULT_KEEP,
    }


@router.post("/volumes/{volume}", status_code=202, summary="Back up a volume")
async def back_up(
    node_id: str,
    request: Request,
    body: BackupIn,
    volume: str = Volume,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Start a backup of a local volume; it runs in the background.

    Rotation deletes archives, which is `backups.manage` territory: a different number to keep
    than the default needs that right too (the default is the baseline policy).
    """
    authorise(conn, registry, who, node_id, "backups.create")
    if body.keep != service.DEFAULT_KEEP:
        who.require("backups.manage", registry.rights(), f"node:{node_id}")
    try:
        job = await service.start(
            request.app,
            node_id,
            who.id,
            "backup",
            volume,
            keep=body.keep,
            actor_id=_actor(request),
            actor=who.actor,
        )
    except ApiError as error:
        _audit(conn, registry, who, "backup.start", node_id, volume=volume, code=error.code)
        raise
    _audit(conn, registry, who, "backup.start", node_id, kind="backup", volume=volume)
    return {"job": job}


@router.post("/restore", status_code=202, summary="Restore an archive into a volume")
async def restore(
    node_id: str,
    request: Request,
    body: RestoreIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Restore into a volume that no container uses; files of the archive overwrite same names.

    The volume name must be typed again, so a restore is never one stray click.
    """
    authorise(conn, registry, who, node_id, "backups.manage")
    details = {
        "kind": "restore",
        "volume": body.volume,
        "archive": body.archive,
        "clear": body.clear,
    }
    if body.confirm != body.volume:
        _audit(
            conn, registry, who, "backup.start", node_id, code="backups.confirm_mismatch", **details
        )
        raise ApiError("backups.confirm_mismatch", 422)
    made_from = service.ARCHIVE_VOLUME.fullmatch(body.archive)
    if made_from and made_from["volume"] != body.volume and not body.other:
        _audit(conn, registry, who, "backup.start", node_id, code="backups.other_volume", **details)
        raise ApiError("backups.other_volume", 422)
    try:
        job = await service.start(
            request.app,
            node_id,
            who.id,
            "restore",
            body.volume,
            archive=body.archive,
            actor_id=_actor(request),
            other=body.other,
            actor=who.actor,
            clear=body.clear,
        )
    except ApiError as error:
        _audit(conn, registry, who, "backup.start", node_id, code=error.code, **details)
        raise
    _audit(conn, registry, who, "backup.start", node_id, **details)
    return {"job": job}


@router.post("/archives/{archive}/download-ticket", summary="Get a one-time download address")
async def download_ticket(
    node_id: str,
    body: DownloadIn,
    request: Request,
    archive: str = ArchiveName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give an address that downloads this archive once, for a minute.

    Needs `backups.download`, your own session (never a key) and a fresh second-factor code.
    The archive is not copied to the hub: it is read from the machine as it is downloaded.
    """
    authorise(conn, registry, who, node_id, "backups.download")
    if who.kind != "session":
        raise MissingRight("backups.not_with_key")
    try:
        second_factor.require_fresh_code(conn, request.app.state.settings, who.id, body.code)
    except ApiError as error:
        if error.code != "auth.too_many_attempts":
            _audit(
                conn, registry, who, "backup.download", node_id, archive=archive, code=error.code
            )
        raise
    token = tickets.issue(request.app, who.id, node_id, archive)
    if token is None:
        raise ApiError("backups.too_many_downloads", 429)
    _audit(conn, registry, who, "backup.download", node_id, archive=archive, ticket="issued")
    return {"ticket": token, "expires_in": tickets.LIFETIME_SECONDS}


@router.get("/archives/{archive}/download", summary="Download an archive (one-time ticket)")
async def download(
    node_id: str,
    request: Request,
    ticket: str = Query(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"),
    archive: str = ArchiveName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
) -> StreamingResponse:
    """Stream the archive from the machine to the browser; nothing is stored on the hub.

    The ticket stands for the person who asked for it. Their right to download is checked
    again now, so a right removed in the last minute stops the download.
    """
    found = tickets.redeem(request.app, ticket, node_id, archive)
    if found is None:
        raise NotFound()
    # A ticket is for a person who is still there: an account disabled in the last minute, or
    # blocked, gets nothing.
    person = users_repo.get_user(conn, found.user_id)
    if person is None or not person["is_active"] or registry.account_blocked(conn, found.user_id):
        raise NotFound()
    who = registry.principal_for(conn, found.user_id)
    authorise(conn, registry, who, node_id, "backups.download")
    running = request.app.state.__dict__.setdefault("downloads_running", {})
    if running.get(node_id, 0) >= MAX_DOWNLOADS_PER_NODE:
        raise ApiError("backups.too_many_downloads", 429)
    try:
        first = await service.ask(
            request.app, node_id, "backups.read", {"archive": archive, "offset": 0}, service.Chunk
        )
    except ApiError as error:
        # An archive made before archives became group-readable cannot be read by the worker.
        if error.code == "worker.forbidden":
            raise ApiError("backups.not_downloadable", 409) from None
        raise
    if first.size > MAX_DOWNLOAD_BYTES:
        raise ApiError("backups.not_downloadable", 409)
    _audit(conn, registry, who, "backup.download", node_id, archive=archive, size=first.size)
    user_id = found.user_id
    sent = [0]

    async def pieces():
        running[node_id] = running.get(node_id, 0) + 1
        try:
            async for data in stream(0, first, time.monotonic(), 0):
                sent[0] += len(data)
                yield data
        finally:
            running[node_id] -= 1
            # How it ended: the whole archive, or how far it got (a closed page, a lost right).
            end = request.app.state.settings.open_db()
            try:
                _audit(
                    end, registry, who, "backup.download", node_id, archive=archive,
                    sent=sent[0], complete=sent[0] == first.size,
                )  # fmt: skip
            finally:
                end.close()

    async def stream(offset, chunk, started, count):
        while True:
            try:
                data = base64.b64decode(chunk.data, validate=True)
            except ValueError:
                return
            # The machine answers whatever it likes: a piece that is empty before the end, or a
            # size that changes along the way, ends the download instead of being passed on.
            if chunk.size != first.size or (not data and offset < first.size):
                return
            if offset + len(data) > first.size:
                return
            offset += len(data)
            if data:
                yield data
            if offset >= first.size:
                return
            count += 1
            if time.monotonic() - started > MAX_DOWNLOAD_SECONDS or (
                count % RECHECK_EVERY == 0 and not _still_allowed(request.app, user_id, node_id)
            ):
                return  # too long, or the right was taken away: the download simply ends
            chunk = await service.ask(
                request.app,
                node_id,
                "backups.read",
                {"archive": archive, "offset": offset},
                service.Chunk,
            )

    return StreamingResponse(
        pieces(),
        media_type="application/gzip",
        headers={
            "Content-Length": str(first.size),
            "Content-Disposition": f'attachment; filename="{archive}"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/archives/{archive}", status_code=204, summary="Delete an archive")
async def delete_archive(
    node_id: str,
    request: Request,
    archive: str = ArchiveName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete one archive from the machine's backup folder."""
    authorise(conn, registry, who, node_id, "backups.manage")
    try:
        await service.ask(
            request.app,
            node_id,
            "backups.delete",
            {"archive": archive},
            _Done,
            _actor(request),
        )
    except ApiError as error:
        _audit(conn, registry, who, "backup.delete", node_id, archive=archive, code=error.code)
        raise
    _audit(conn, registry, who, "backup.delete", node_id, archive=archive)


class _Done(BaseModel):
    model_config = ConfigDict(extra="ignore")
    done: bool
