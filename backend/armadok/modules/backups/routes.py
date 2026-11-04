"""Endpoints of volume backups, per node.

Reading needs `backups.read`, starting a backup `backups.create`. Restoring over a volume and
deleting an archive need `backups.manage` (sensitive). Archives never leave the machine: the
hub lists them, names them and asks the worker to delete or restore them.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.backups import repository as repo
from armadok.modules.backups import service
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
    details = {"kind": "restore", "volume": body.volume, "archive": body.archive}
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
        )
    except ApiError as error:
        _audit(conn, registry, who, "backup.start", node_id, code=error.code, **details)
        raise
    _audit(conn, registry, who, "backup.start", node_id, **details)
    return {"job": job}


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
