"""Node resources endpoint of a node."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from armadok.core.coalesce import Shared
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.workers.access import ask, authorise

router = APIRouter(prefix="/nodes/{node_id}/machine", tags=["machine"])
_shared = Shared(4.0)

_BYTES = Field(ge=0, le=2**62)


class Disk(BaseModel):
    """Free space of one folder the worker was given (a fixed label, never a path)."""

    model_config = ConfigDict(extra="ignore", strict=True)
    name: str = Field(pattern=r"^(projects|traefik|backups|data)$")
    total: int = _BYTES
    free: int = _BYTES

    @model_validator(mode="after")
    def _free_fits(self) -> Disk:
        """Free space cannot exceed the total."""
        if self.free > self.total:
            raise ValueError("free exceeds total")
        return self


class MachineStats(BaseModel):
    """The answer of `machine.stats`. The worker is untrusted: every figure is bounded."""

    model_config = ConfigDict(extra="ignore", strict=True)
    cpu_percent: float = Field(ge=0, le=100)
    cpu_count: int = Field(ge=1, le=4096)
    load: list[Annotated[float, Field(ge=0, le=1e6)]] = Field(min_length=3, max_length=3)
    memory_total: int = _BYTES
    memory_available: int = _BYTES
    swap_total: int = _BYTES
    swap_free: int = _BYTES
    uptime_seconds: int = Field(ge=0, le=10**10)
    network_received: int = _BYTES
    network_sent: int = _BYTES
    temperature: float | None = Field(default=None, ge=-50, le=200)
    disks: list[Disk] = Field(max_length=8)

    @model_validator(mode="after")
    def _consistent(self) -> MachineStats:
        """Available memory and free swap cannot exceed their totals."""
        if self.memory_available > self.memory_total or self.swap_free > self.swap_total:
            raise ValueError("available exceeds total")
        return self


@router.get("", summary="Resources of the node")
async def machine(
    node_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return processor, memory, load, disks, network counters, uptime and temperature."""
    authorise(conn, registry, who, node_id, "machine.read")
    # Many viewers polling one node cost one measurement (the check above ran for each).
    return await _shared.get(
        node_id,
        lambda: _measure(request, registry, node_id),
    )


async def _measure(request: Request, registry: Registry, node_id: str) -> dict:
    answer = await ask(request, registry, node_id, "machine.stats", {}, MachineStats)
    return answer.model_dump()
