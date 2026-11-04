"""Endpoints for the vulnerabilities of Armadok's own components (platform administration).

Reading needs `vulns.read`; scanning and recording a justified exception need `vulns.manage`.
Both are sensitive platform rights: the list says exactly what could be attacked.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.vulns import repository, service

router = APIRouter(prefix="/vulns", tags=["vulns"])
MIN_SCAN_GAP_SECONDS = 60
_last_manual = [-1e9]


class IgnoreIn(BaseModel):
    """Why a finding is accepted for now (kept in the audit log)."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")


def _audit(conn, registry, who, event, outcome="success", **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="vulnerability",
        outcome=outcome,
        details=details,
    )


@router.get("", summary="Known vulnerabilities in Armadok's components")
def overview(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the findings, the last scan and whether the check is switched on."""
    who.require("vulns.read", registry.rights())
    return {
        "enabled": request.app.state.settings.vuln_check,
        "scan": repository.last_scan(conn),
        "findings": repository.findings(conn),
        "can_manage": who.has("vulns.manage", registry.rights()),
    }


@router.post("/scan", summary="Check now")
async def scan_now(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Ask the vulnerability database now (package names and versions are sent, nothing else)."""
    who.require("vulns.manage", registry.rights())
    if not request.app.state.settings.vuln_check:
        raise ApiError("vulns.disabled", 409)
    if time.monotonic() - _last_manual[0] < MIN_SCAN_GAP_SECONDS:
        raise ApiError("vulns.too_soon", 429)
    try:
        announced = await service.run_scan(request.app)
    except service.ScanFailed as error:
        if error.code == "vulns.busy":  # someone else's scan is running: not a failure
            raise ApiError("vulns.busy", 409) from None
        _audit(conn, registry, who, "vulns.scan", "failure", code=error.code)
        raise ApiError(error.code, 502) from None
    _last_manual[0] = time.monotonic()  # a failed attempt does not make the next one wait
    _audit(conn, registry, who, "vulns.scan", announced=announced)
    return {"announced": announced}


@router.put("/{finding_id}/ignore", summary="Accept a finding for now, with a reason")
def ignore(
    finding_id: int,
    body: IgnoreIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Record a justified exception: the finding stays visible, marked as accepted."""
    who.require("vulns.manage", registry.rights())
    if repository.get(conn, finding_id) is None:
        raise NotFound()
    if not repository.ignore(conn, finding_id, body.reason, who.id):
        raise ApiError("vulns.already_accepted", 409)
    _audit(conn, registry, who, "vulns.ignore", finding=finding_id, reason=body.reason)
    return {"ignored": True}


@router.delete("/{finding_id}/ignore", status_code=204, summary="Stop accepting a finding")
def unignore(
    finding_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove the exception from a finding."""
    who.require("vulns.manage", registry.rights())
    before = repository.get(conn, finding_id)
    if before is None or not repository.ignore(conn, finding_id, None, who.id):
        raise NotFound()
    _audit(
        conn,
        registry,
        who,
        "vulns.unignore",
        finding=finding_id,
        previous_reason=before["ignored_reason"],
    )
