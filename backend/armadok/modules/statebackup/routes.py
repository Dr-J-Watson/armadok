"""Endpoints for backups of the hub's own state (platform administration).

All of it needs the sensitive platform right `state_backup.manage`, which gives no access to any
machine. Downloading a backup needs a person's own session and a fresh second-factor code:
even encrypted, the file holds every account and every secret.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit, crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.statebackup import archive
from armadok.modules.users import second_factor

router = APIRouter(prefix="/statebackups", tags=["statebackup"])

MIN_GAP_SECONDS = 30
_last_manual = [-1e9]
BackupName = Path(pattern=archive.NAME.pattern)


class DownloadIn(BaseModel):
    """The fresh second-factor code that unlocks a download."""

    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=32)


def _audit(conn, registry, who, event, outcome="success", **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="state_backup",
        outcome=outcome,
        details=details,
    )


@router.get("", summary="Backups of the hub's own state")
def list_backups(
    request: Request,
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the backups and the settings that govern them."""
    who.require("state_backup.manage", registry.rights())
    settings = request.app.state.settings
    return {
        "backups": archive.listing(settings),
        "keep": settings.state_backup_keep,
        "every_hours": settings.state_backup_hours,
        "ready": bool(settings.state_backup_key or settings.encryption_key),
        "separate_key": bool(settings.state_backup_key),
        "custom_folder": settings.state_backup_dir is not None,
    }


@router.post("/new-key", summary="Generate a key of its own for these backups")
def new_key(
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return a new random key, once, to be written down and set as `ARMADOK_STATE_BACKUP_KEY`.

    Nothing is stored here: the key is the one thing a backup cannot contain, and keeping it in
    the database would defeat the point. Only your own session may ask, never an API key.
    """
    who.require("state_backup.manage", registry.rights())
    if who.kind != "session":
        raise MissingRight("statebackup.not_with_key")
    return {"key": crypto.generate_key()}


@router.post("", status_code=201, summary="Back up now")
async def create_backup(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Make a backup now (it is encrypted with the encryption key)."""
    who.require("state_backup.manage", registry.rights())
    if time.monotonic() - _last_manual[0] < MIN_GAP_SECONDS:
        raise ApiError("statebackup.too_soon", 429)
    try:
        name = await asyncio.to_thread(archive.create, request.app.state.settings)
    except ApiError as error:
        _audit(conn, registry, who, "statebackup.create", "failure", code=error.code)
        raise
    except OSError:
        _audit(
            conn, registry, who, "statebackup.create", "failure", code="statebackup.write_failed"
        )
        raise ApiError("statebackup.write_failed", 500) from None
    _last_manual[0] = time.monotonic()  # a failed attempt does not make the next one wait
    _audit(conn, registry, who, "statebackup.create", name=name)
    return {"name": name}


@router.delete("/{name}", status_code=204, summary="Delete a backup")
def delete_backup(
    request: Request,
    name: str = BackupName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete one backup."""
    who.require("state_backup.manage", registry.rights())
    archive.remove(request.app.state.settings, name)
    _audit(conn, registry, who, "statebackup.delete", name=name)


@router.post("/{name}/download", summary="Download a backup")
def download_backup(
    request: Request,
    body: DownloadIn,
    name: str = BackupName,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> FileResponse:
    """Send the encrypted backup file. Needs your own session and a fresh second-factor code."""
    who.require("state_backup.manage", registry.rights())
    if who.kind != "session":
        raise MissingRight("statebackup.not_with_key")
    settings = request.app.state.settings
    path = archive.path_of(settings, name)
    try:
        second_factor.require_fresh_code(conn, settings, who.id, body.code)
    except ApiError as error:
        if error.code != "auth.too_many_attempts":
            _audit(
                conn, registry, who, "statebackup.download", "failure", name=name, code=error.code
            )
        raise
    _audit(conn, registry, who, "statebackup.download", name=name)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
        headers={"Cache-Control": "no-store"},
    )
