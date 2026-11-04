"""Endpoints of scheduled tasks, per node.

Reading needs `schedule.read`. Creating, changing, running and removing a task needs
`schedule.manage` (sensitive: the task will act later, unattended, as its creator) and, at
creation, the right the action itself needs on that machine: nobody schedules what they could
not do by hand. The same right is checked again at each run.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.schedule import repository as repo
from armadok.modules.schedule import runner, timing
from armadok.modules.workers.access import authorise

router = APIRouter(prefix="/nodes/{node_id}/schedule", tags=["schedule"])

MAX_PER_NODE = 50
MAX_TOTAL = 500
MIN_RUN_GAP_SECONDS = 10
_last_manual: dict[str, float] = {}
TaskId = Path(pattern=r"^[0-9a-f]{16}$")


class TaskIn(BaseModel):
    """A task to create: what to do, to what, and when."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[^\x00-\x1f\x7f]{1,60}$")
    action: str = Field(max_length=60)
    target: dict[str, str] = Field(default_factory=dict)
    schedule: dict[str, Any]


class TaskChange(BaseModel):
    """What can be changed afterwards: the name, the switch and the schedule."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, pattern=r"^[^\x00-\x1f\x7f]{1,60}$")
    enabled: bool | None = None
    schedule: dict[str, Any] | None = None


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ApiError("schedule.invalid_name", 422)
    return name


def _schedule(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        return timing.validate(spec)
    except ValueError:
        raise ApiError("schedule.invalid_schedule", 422) from None


def _task_of(conn: sqlite3.Connection, node_id: str, task_id: str) -> sqlite3.Row:
    row = repo.get(conn, task_id)
    if row is None or row["node_id"] != node_id:
        raise NotFound()
    return row


def _may_touch(registry: Registry, who: Principal, row: sqlite3.Row) -> None:
    """Refuse unless the caller could do the task's action by hand on this machine.

    A task acts with its creator's rights: whoever runs, retimes or removes it must hold the
    action's right too, or "schedule.manage" would borrow rights from the creator.
    """
    action = registry.hook_actions().get(row["action"])
    if action is not None:
        who.require(action.right, registry.rights(), f"node:{row['node_id']}")


def _audit(conn, registry, who, event, node_id, task_id, **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="scheduled_task",
        object_id=task_id,
        details={"node_id": node_id, **details},
    )


@router.get("", summary="Scheduled tasks of a node")
def list_tasks(
    node_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the tasks, and the actions that can be scheduled with the fields they need."""
    authorise(conn, registry, who, node_id, "schedule.read")
    scope = f"node:{node_id}"
    catalogue = registry.rights()
    actions = [
        {"name": a.name, "fields": [{"name": f, "choices": list(c)} for f, c in a.fields]}
        for a in registry.hook_actions().values()
        if a.schedulable and who.has(a.right, catalogue, scope)
    ]
    tasks = []
    for r in repo.list_for_node(conn, node_id):
        shown = repo.view(r)
        action = registry.hook_actions().get(r["action"])
        if action is None or not who.has(action.right, catalogue, scope):
            # What a task targets is for those who could do it themselves.
            shown["target"], shown["created_by"] = {}, None
        tasks.append(shown)
    return {
        "tasks": tasks,
        "actions": sorted(actions, key=lambda a: a["name"]),
        "can_manage": who.has("schedule.manage", catalogue, scope),
    }


@router.post("", status_code=201, summary="Create a scheduled task")
def create_task(
    node_id: str,
    body: TaskIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a task. You must hold, on this machine, the right the action needs."""
    authorise(conn, registry, who, node_id, "schedule.manage")
    action = registry.hook_actions().get(body.action)
    if action is None or not action.schedulable:
        raise ApiError("schedule.unknown_action", 422)
    if "node_id" in body.target:  # the machine is the one in the address, nothing else
        raise ApiError("schedule.invalid_target", 422)
    try:
        target = action.validate({**body.target, "node_id": node_id})
    except ValueError:
        raise ApiError("schedule.invalid_target", 422) from None
    if target.get("node_id") != node_id:
        raise ApiError("schedule.invalid_target", 422)
    who.require(action.right, registry.rights(), f"node:{node_id}")
    schedule = _schedule(body.schedule)
    if repo.count(conn, node_id) >= MAX_PER_NODE or repo.count(conn) >= MAX_TOTAL:
        raise ApiError("schedule.too_many", 409)
    task_id = repo.create(
        conn, node_id, who.id, _clean_name(body.name), body.action, target, schedule
    )
    _audit(conn, registry, who, "schedule.create", node_id, task_id, action=body.action)
    return repo.view(repo.get(conn, task_id))


@router.patch("/{task_id}", summary="Change a scheduled task")
def change_task(
    node_id: str,
    body: TaskChange,
    task_id: str = TaskId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Rename, switch on or off, or retime a task. The action and target never change."""
    authorise(conn, registry, who, node_id, "schedule.manage")
    _may_touch(registry, who, _task_of(conn, node_id, task_id))
    sent = body.model_dump(exclude_unset=True)
    repo.update(
        conn,
        task_id,
        _clean_name(body.name) if body.name is not None else None,
        body.enabled,
        _schedule(body.schedule) if body.schedule is not None else None,
    )
    _audit(conn, registry, who, "schedule.update", node_id, task_id, fields=",".join(sorted(sent)))
    return repo.view(repo.get(conn, task_id))


@router.delete("/{task_id}", status_code=204, summary="Delete a scheduled task")
def delete_task(
    node_id: str,
    task_id: str = TaskId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove a task."""
    authorise(conn, registry, who, node_id, "schedule.manage")
    _may_touch(registry, who, _task_of(conn, node_id, task_id))
    repo.delete(conn, task_id)
    _audit(conn, registry, who, "schedule.delete", node_id, task_id)


class MissedDecision(BaseModel):
    """What to do about a run missed while Armadok was stopped."""

    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(run|wait)$")


@router.post("/{task_id}/missed", summary="Decide about a missed run")
async def decide_missed(
    node_id: str,
    body: MissedDecision,
    request: Request,
    task_id: str = TaskId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Run the missed task now, or leave it for its next time. Only the first answer counts."""
    authorise(conn, registry, who, node_id, "schedule.manage")
    row = _task_of(conn, node_id, task_id)
    _may_touch(registry, who, row)
    slot = row["missed_slot"]
    if slot is None or not repo.clear_missed(conn, task_id, slot):
        raise ApiError("schedule.nothing_missed", 409)
    _audit(
        conn, registry, who, "schedule.missed_decision", node_id, task_id, decision=body.decision
    )
    if body.decision == "wait":
        return {"result": "waiting"}
    result = await runner.run_task(request.app, row, requested_by=who.id)
    if result == "schedule.already_running":
        # Nothing ran, so the question is still open.
        conn.execute("UPDATE scheduled_tasks SET missed_slot = ? WHERE id = ?", (slot, task_id))
        conn.commit()
    return {"result": result}


@router.post("/{task_id}/run", summary="Run a task now")
async def run_now(
    node_id: str,
    request: Request,
    task_id: str = TaskId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Run the task once now, as its creator would; return how it went.

    The task acts with its creator's rights, and the caller must hold the action's right too.
    The caller is named in the audit entry of the run.
    """
    authorise(conn, registry, who, node_id, "schedule.manage")
    row = _task_of(conn, node_id, task_id)
    _may_touch(registry, who, row)
    now = time.monotonic()
    if now - _last_manual.get(task_id, -1e9) < MIN_RUN_GAP_SECONDS:
        raise ApiError("schedule.too_soon", 429)
    _last_manual[task_id] = now
    if len(_last_manual) > 5000:
        for key in [k for k, t in _last_manual.items() if now - t > MIN_RUN_GAP_SECONDS]:
            del _last_manual[key]
    _audit(conn, registry, who, "schedule.run_now", node_id, task_id)
    result = await runner.run_task(request.app, row, requested_by=who.id)
    return {"result": result}
