"""Endpoints of incoming hooks.

Managing hooks (`hooks.manage`, sensitive) needs the right the hook's action needs on its
target, so nobody builds a hook that does more than they could do. Firing a hook needs no
login, only its own token: the hook is the one place where a tool without a key may start a
deployment, and it can only ever do the one thing it was made for. It acts as its creator, and
only while the creator is still active and still holds that right.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.core.throttle import FailureLimiter
from armadok.modules.hooks import repository as repo
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.users import repository as users_repo
from armadok.modules.users.security import token_hash

router = APIRouter(prefix="/hooks", tags=["hooks"])

MIN_INTERVAL_SECONDS = 5  # a hook cannot be hammered; the project is also busy while it runs
FAILURES_PER_MINUTE = 10  # wrong tokens per source address
FAILURES_PER_HOOK = 30  # wrong tokens for one hook, from anywhere
MAX_TRACKED = 5000
limiter = FailureLimiter({"ip": FAILURES_PER_MINUTE, "hook": FAILURES_PER_HOOK}, MAX_TRACKED)
_audited: dict[str, float] = {}
HookId = Path(pattern=r"^[0-9a-f]{16}$")


class HookIn(BaseModel):
    """A hook: what it does, to what, and until when (optional)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[^\x00-\x1f\x7f]{1,60}$")
    action: str = Field(max_length=60)
    target: dict[str, str]
    expires_at: AwareDatetime | None = None


def _audit(conn, registry, actor, event, hook_id, outcome="success", **details):
    audit.record(
        conn,
        registry.audit_events(),
        actor,
        event,
        object_type="hook",
        object_id=hook_id,
        outcome=outcome,
        details=details,
    )


def _audit_attempt(conn, registry, row, hook_id: str, code: str) -> None:
    """Audit a refused call to a hook that exists, at most once a minute per hook.

    A flood of guesses must not flood the audit log; an attacker cannot make entries for
    hooks that do not exist.
    """
    now = time.monotonic()
    if row is None or now - _audited.get(hook_id, -61) < 60:
        return
    _audited[hook_id] = now
    if len(_audited) > MAX_TRACKED:
        _audited.clear()
    _audit(
        conn,
        registry,
        f"{row['created_by']} via key:hook-{hook_id}",
        "hook.fire",
        hook_id,
        "failure",
        code=code,
    )


@router.get("", summary="Your hooks")
def list_hooks(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return your own hooks (never their tokens) and the actions a hook can have."""
    who.require("hooks.manage", registry.rights())
    return {
        "hooks": repo.list_for(conn, who.id),
        "actions": sorted(n for n, a in registry.hook_actions().items() if a.hookable),
    }


@router.post("", status_code=201, summary="Create a hook")
def create_hook(
    body: HookIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a hook and return its token, shown only in this answer.

    You must hold, on the target machine, the right the action needs.
    """
    catalogue = registry.rights()
    who.require("hooks.manage", catalogue)
    action = registry.hook_actions().get(body.action)
    if action is None or not action.hookable:
        raise ApiError("hooks.unknown_action", 422)
    try:
        target = action.validate(dict(body.target))
    except ValueError:
        raise ApiError("hooks.invalid_target", 422) from None
    node_id = target.get("node_id")
    scope = f"node:{node_id}"
    # An invisible machine is "not found", as everywhere; then the action's own right.
    if node_id is None or nodes_repo.get_node(conn, node_id) is None:
        raise NotFound()
    if not who.has("nodes.read", catalogue, scope):
        raise NotFound()
    who.require(action.right, catalogue, scope)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise ApiError("hooks.invalid_expiry", 422)
    hook_id, token = repo.create(
        conn, body.name.strip(), body.action, target, who.id, body.expires_at
    )
    _audit(
        conn,
        registry,
        who.actor,
        "hook.create",
        hook_id,
        name=body.name.strip(),
        action=body.action,
    )
    return {"id": hook_id, "token": token, "fire_path": f"/api/v1/hooks/{hook_id}/fire"}


@router.delete("/{hook_id}", status_code=204, summary="Revoke a hook")
def revoke_hook(
    hook_id: str = HookId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Revoke one of your hooks: its token stops working at once."""
    who.require("hooks.manage", registry.rights())
    if not repo.revoke(conn, hook_id, who.id):
        raise NotFound()
    _audit(conn, registry, who.actor, "hook.revoke", hook_id)


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


@router.post("/{hook_id}/fire", status_code=202, summary="Fire a hook")
async def fire(
    request: Request,
    hook_id: str = HookId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
) -> dict:
    """Run the hook's action. The token goes in `Authorization: Bearer <token>`.

    Every refusal looks the same (401) so nobody can tell a wrong token from a revoked or
    unknown hook. The action runs as the hook's creator, who must still hold its right.
    """
    source = request.client.host if request.client else ""
    token = _bearer(request)
    row = repo.get(conn, hook_id)
    # Always compare something, so a missing hook costs the same as a wrong token.
    stored = row["token_hash"] if row else token_hash("no such hook")
    matches = hmac.compare_digest(stored, token_hash(token or ""))
    expires = datetime.fromisoformat(row["expires_at"]) if row and row["expires_at"] else None
    if row is None or not matches or row["revoked"] or (expires and expires <= datetime.now(UTC)):
        # Only wrong tokens are slowed down: the right one is never locked out by someone
        # else's guessing (behind a proxy everybody may share one address).
        if limiter.blocked(ip=source, hook=hook_id):
            raise ApiError("hooks.too_many_attempts", 429)
        limiter.remember(ip=source, hook=hook_id)
        _audit_attempt(conn, registry, row, hook_id, "hooks.invalid")
        raise ApiError("hooks.invalid", 401)
    actor = f"{row['created_by']} via key:hook-{hook_id}"
    action = registry.hook_actions().get(row["action"])
    if action is not None and not action.hookable:
        action = None
    creator = users_repo.get_user(conn, row["created_by"])
    # An account disabled since the hook was made takes its hooks with it for good.
    disabled_since = creator and creator["deactivated_at"]
    if (
        action is None
        or creator is None
        or not creator["is_active"]
        or (disabled_since and disabled_since > row["created_at"])
        # A service account stops with the person responsible for it.
        or registry.account_blocked(conn, row["created_by"])
    ):
        _audit(conn, registry, actor, "hook.fire", hook_id, "failure", code="hooks.unavailable")
        raise ApiError("hooks.unavailable", 403)
    target = json.loads(row["target"])
    who = registry.principal_for(conn, row["created_by"], kind="api_key", key_id=f"hook-{hook_id}")
    # The creator may have lost the right since the hook was made.
    if not who.has(action.right, registry.rights(), f"node:{target.get('node_id')}"):
        repo.set_result(conn, hook_id, "hooks.creator_lost_right")
        _audit(
            conn, registry, actor, "hook.fire", hook_id, "failure", code="hooks.creator_lost_right"
        )
        raise ApiError("hooks.creator_lost_right", 403)
    # Taken only now, so a call refused above does not make the next one wait.
    if not repo.claim_call(conn, hook_id, MIN_INTERVAL_SECONDS):
        _audit(conn, registry, actor, "hook.fire", hook_id, "failure", code="hooks.too_often")
        raise ApiError("hooks.too_often", 429)
    try:
        await action.run(request.app, who, target)
    except ApiError as error:
        repo.set_result(conn, hook_id, error.code)
        _audit(conn, registry, actor, "hook.fire", hook_id, "failure", code=error.code)
        raise
    repo.set_result(conn, hook_id, "started")
    _audit(conn, registry, actor, "hook.fire", hook_id, action=row["action"], **target)
    return {"started": True}
