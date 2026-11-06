"""Running scheduled tasks: the background loop and the one function that runs a task.

A task acts as the person who made it, exactly like an incoming hook: they must still be an
active account and still hold the action's right on the node at every run. A task that
cannot run says why (an error code) and stays; nothing is ever retried in a burst.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from armadok.core import audit
from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.core.signals import announce
from armadok.modules.schedule import repository as repo
from armadok.modules.users import repository as users_repo

log = logging.getLogger("armadok.schedule")
TICK_SECONDS = 30
STARTUP_DELAY_SECONDS = 60
MAX_PER_TICK = 10
RUN_TIMEOUT_SECONDS = 120  # one stuck run must not hold up the others (or the next round)
# A run missed because the hub was down is made up for only if it is not too late: a nightly
# prune must not start at noon.
MAX_LATE = timedelta(hours=1)  # later than this, the task's manager decides
_running: set[str] = set()


def _audit(conn, registry, actor, event, task, outcome="success", **details) -> None:
    audit.record(
        conn,
        registry.audit_events(),
        actor,
        event,
        object_type="scheduled_task",
        object_id=task["id"],
        outcome=outcome,
        details={"node_id": task["node_id"], "action": task["action"], **details},
    )


async def run_task(app: Any, task: Any, requested_by: str | None = None) -> str:
    """Run one task now and return its result: `ok` or an error code. Never raises.

    `requested_by` names the person who asked for a run outside the schedule; it goes into the
    audit entry of the run itself.
    """
    if task["id"] in _running:
        return "schedule.already_running"
    _running.add(task["id"])
    try:
        return await asyncio.wait_for(_run(app, task, requested_by), RUN_TIMEOUT_SECONDS)
    except TimeoutError:
        return "schedule.timeout"
    finally:
        _running.discard(task["id"])


async def _run(app: Any, task: Any, requested_by: str | None = None) -> str:
    registry = app.state.registry
    conn = app.state.settings.open_db()
    try:
        actor = f"{task['created_by']} via schedule:{task['id']}"
        action = registry.hook_actions().get(task["action"])
        creator = users_repo.get_user(conn, task["created_by"])
        since = creator and creator["deactivated_at"]
        if action is None or not action.schedulable:
            result = "schedule.action_unavailable"
        elif creator is None or not creator["is_active"] or (since and since > task["created_at"]):
            result = "schedule.creator_unavailable"
        else:
            target = json.loads(task["target"])
            who = registry.principal_for(
                conn, task["created_by"], kind="api_key", key_id=f"schedule-{task['id']}"
            )
            if not who.has(action.right, registry.rights(), f"node:{task['node_id']}"):
                result = "schedule.creator_lost_right"
            else:
                try:
                    await action.run(app, who, target)
                    result = "ok"
                except ApiError as error:
                    result = error.code
                except Exception:  # noqa: BLE001  (details stay in the log)
                    log.exception("scheduled task %s failed", task["id"])
                    result = "schedule.failed"
        repo.set_result(conn, task["id"], result)
        _audit(
            conn,
            registry,
            actor,
            "schedule.run",
            task,
            "success" if result == "ok" else "failure",
            **({} if result == "ok" else {"code": result}),
            **({"requested_by": requested_by} if requested_by else {}),
        )
        node_name = conn.execute(
            "SELECT name FROM nodes WHERE id = ?", (task["node_id"],)
        ).fetchone()
    finally:
        conn.close()
    await announce(app, "schedule.changed", task["node_id"])
    if result != "ok":
        await emit(
            app,
            "notify",
            event="schedule.failed",
            node_id=task["node_id"],
            params={
                "node": node_name["name"] if node_name else "-",
                "name": task["name"],
                "code": result,
            },
        )
    return result


INACTIVE = timedelta(days=30)  # a manager unseen this long is replaced by the node's owner


def _stand_in(conn, task: Any, now: datetime) -> str | None:
    """Return the node owner when the task's manager cannot answer, else None.

    The manager cannot answer if their account is disabled or has not done anything (the audit
    log is the only record of activity) for a month. Nobody is added if the owner is the manager.
    """
    owner = conn.execute("SELECT owner FROM nodes WHERE id = ?", (task["node_id"],)).fetchone()
    if owner is None or owner["owner"] == task["created_by"]:
        return None
    user = conn.execute(
        "SELECT is_active FROM users WHERE id = ?", (task["created_by"],)
    ).fetchone()
    last = conn.execute(
        "SELECT MAX(at) AS at FROM audit_log WHERE actor = ? AND outcome = 'success'",
        (task["created_by"],),
    ).fetchone()["at"]
    gone = user is None or not user["is_active"]
    idle = last is None or now - datetime.fromisoformat(last) > INACTIVE
    return owner["owner"] if gone or idle else None


async def _node_name(conn, node_id: str) -> str:
    row = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row["name"] if row else "-"


async def run_due(app: Any) -> int:
    """Run the tasks whose time has come; return how many ran (or were skipped).

    A run that is more than an hour late (the hub was stopped) is not made up for on its own: its
    manager is asked whether to run it now or wait. If they have not answered by the task's next
    time, the question is replaced by a notice that the run was missed.
    """
    now = datetime.now(UTC)
    conn = app.state.settings.open_db()
    notices: list[tuple[str, Any, str, str]] = []
    try:
        due = repo.due(conn, now, MAX_PER_TICK)
        taken, late = [], []
        for task in due:
            # Moved on first, so a slow run (or a crash) can never make one slot run twice.
            if not repo.advance(conn, task):
                continue  # another loop took this slot
            taken.append(task)
            if task["missed_slot"] and repo.clear_missed(conn, task["id"], task["missed_slot"]):
                notices.append(("schedule.missed_final", task, task["missed_slot"], ""))
            if now - datetime.fromisoformat(task["next_run"]) > MAX_LATE:
                repo.set_skipped(conn, task["id"], task["next_run"])
                _audit(
                    conn,
                    app.state.registry,
                    f"{task['created_by']} via schedule:{task['id']}",
                    "schedule.run",
                    task,
                    "failure",
                    code="schedule.skipped_late",
                )
                late.append(task["id"])
                notices.append(("schedule.missed", task, task["next_run"], ""))
        stand_ins = {
            t["id"]: _stand_in(conn, t, now) for e, t, _, _ in notices if e == "schedule.missed"
        }
        names = {t["node_id"]: await _node_name(conn, t["node_id"]) for _, t, _, _ in notices}
    finally:
        conn.close()
    for event, task, slot, _ in notices:
        await emit(
            app,
            "notify",
            event=event,
            node_id=task["node_id"],
            user_ids=[u for u in (task["created_by"], stand_ins.get(task["id"])) if u],
            params={"node": names[task["node_id"]], "name": task["name"], "slot": slot[:16]},
        )
    await asyncio.gather(*(run_task(app, t) for t in taken if t["id"] not in late))
    return len(taken)


async def watch_loop(app: Any) -> None:
    """Run due tasks every half minute until the application stops."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await run_due(app)
        except Exception:  # noqa: BLE001  (keep the loop alive whatever happens)
            log.exception("schedule round failed")
        await asyncio.sleep(TICK_SECONDS)
