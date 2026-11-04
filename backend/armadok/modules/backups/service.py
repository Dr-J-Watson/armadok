"""Starting backup jobs and following them to their end."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from armadok.core import audit
from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.core.signals import announce
from armadok.modules.backups import repository as repo
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.workers.routes import call_operation

log = logging.getLogger("armadok.backups")
VOLUME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ARCHIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}--\d{8}T\d{6}Z\.tar\.gz$")
ARCHIVE_VOLUME = re.compile(r"^(?P<volume>.+)--\d{8}T\d{6}Z\.tar\.gz$")
DEFAULT_KEEP = 7


class Started(BaseModel):
    """The worker's answer to starting a job (untrusted)."""

    model_config = ConfigDict(extra="ignore")
    task: str = Field(pattern=r"^[0-9a-f]{16}$")


class TaskState(BaseModel):
    """The worker's answer about a job (untrusted: every field is checked)."""

    model_config = ConfigDict(extra="ignore")
    state: str = Field(pattern=r"^(running|success|failed)$")
    error: str | None = Field(default=None, pattern=r"^[a-z0-9_.]{1,60}$")
    archive: str | None = Field(default=None, pattern=ARCHIVE.pattern)
    size: int | None = Field(default=None, ge=0, le=2**50)


class Archive(BaseModel):
    """One archive in the worker's backup folder."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(pattern=ARCHIVE.pattern)
    volume: str = Field(pattern=VOLUME.pattern)
    stamp: str = Field(pattern=r"^\d{8}T\d{6}Z$")
    size: int = Field(ge=0, le=2**50)


class Archives(BaseModel):
    """The worker's list of archives."""

    model_config = ConfigDict(extra="ignore")
    archives: list[Archive] = Field(max_length=2000)
    truncated: bool = False


async def ask(
    app: Any,
    node_id: str,
    op: str,
    args: dict[str, Any],
    model: type[BaseModel],
    actor_id: str | None = None,
) -> Any:
    """Run an operation on the node's worker and validate the (untrusted) answer."""
    result = await call_operation(app, app.state.registry, node_id, op, args, actor_id)
    try:
        return model.model_validate(result)
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None


async def start(
    app: Any,
    node_id: str,
    who_id: str,
    kind: str,
    volume: str,
    archive: str | None = None,
    keep: int = DEFAULT_KEEP,
    actor_id: str | None = None,
    other: bool = False,
    actor: str | None = None,
) -> str:
    """Start a backup (`kind` "backup") or a restore ("restore"); return the job id."""
    if kind == "backup":
        op, args = "backups.start", {"volume": volume, "keep": keep}
    else:
        op, args = "backups.restore", {"volume": volume, "archive": archive, "other": other}
    started = await ask(app, node_id, op, args, Started, actor_id)
    conn = app.state.settings.open_db()
    try:
        job_id = repo.create(
            conn, node_id, kind, volume, archive, started.task, who_id, actor or who_id
        )
    finally:
        conn.close()
    await announce(app, "backups.changed", node_id)
    return job_id


def _audit_end(conn, registry, row, ok: bool, error: str | None) -> None:
    audit.record(
        conn,
        registry.audit_events(),
        row["actor"] or row["started_by"],
        "backup.done",
        object_type="node",
        object_id=row["node_id"],
        outcome="success" if ok else "failure",
        details={
            "kind": row["kind"],
            "volume": row["volume"],
            **({} if ok else {"code": error or "worker.op_failed"}),
        },
    )


async def sync_job(app: Any, row: sqlite3.Row) -> None:
    """Ask the worker how a running job is doing and record its end once."""
    if repo.is_stale(row):
        result = TaskState(state="failed", error="worker.timeout")
    else:
        try:
            result = await ask(
                app, row["node_id"], "backups.task", {"task": row["worker_task"]}, TaskState
            )
        except ApiError as error:
            if error.code != "worker.not_found":
                return  # the worker is away: try again later
            result = TaskState(state="failed", error="worker.not_found")
    if result.state == "running":
        return
    ok = result.state == "success"
    registry = app.state.registry
    conn = app.state.settings.open_db()
    try:
        moved = repo.finish(
            conn, row["id"], result.state, None if ok else result.error, result.archive, result.size
        )
        node = nodes_repo.get_node(conn, row["node_id"])
        if moved:
            _audit_end(conn, registry, row, ok, result.error)
    finally:
        conn.close()
    if not moved:
        return
    await announce(app, "backups.changed", row["node_id"])
    await emit(
        app,
        "notify",
        event="backup.done" if ok else "backup.failed",
        node_id=row["node_id"],
        params={
            "node": node["name"] if node else "-",
            "volume": row["volume"],
            "code": result.error or "-",
        },
    )
