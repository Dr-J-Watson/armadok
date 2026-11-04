"""Endpoints to give an object to someone, accept or refuse a gift, and take objects back.

Rules (see the specification): the owner of an object may give it; the receiver must accept;
the owner of the machine may take back anything inside it, never the other way round. Giving
and taking are sensitive: a person's own session (never an API key), the fresh second-factor
code when the account has one, and an audit entry. Notes stay private to their author: the giver
alone decides whether to hand over theirs, and taking never moves any.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from armadok.core import audit
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.core.throttle import FailureLimiter
from armadok.modules.nodes import repository as nodes_repo
from armadok.modules.ownership import repository, service
from armadok.modules.roles.service import atomic
from armadok.modules.users import repository as users_repo
from armadok.modules.users import second_factor
from armadok.modules.workers.access import authorise

router = APIRouter(tags=["ownership"])

# Looking a person up by name: a few tries a minute, so it cannot list who sees a machine.
people_limiter = FailureLimiter({"user": 10})

NodeId = Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
OfferId = Path(pattern=r"^[0-9a-f]{32}$")


class Target(BaseModel):
    """Which objects: one (`object_type` + `object_id`) or everything of the caller (`cascade`)."""

    model_config = ConfigDict(extra="forbid")
    object_type: str | None = Field(default=None, pattern=r"^[a-z_]{1,30}$")
    object_id: str | None = Field(default=None, max_length=128)
    cascade: bool = False
    # Only for a cascade over one kind ("all my projects"); empty means every kind.
    only_type: str | None = Field(default=None, pattern=r"^[a-z_]{1,30}$")

    @model_validator(mode="after")
    def one_or_all(self) -> Target:
        """Exactly one of: a single object, or a cascade."""
        single = self.object_type is not None and self.object_id is not None
        if self.cascade == single or (self.object_type is None) != (self.object_id is None):
            raise ValueError("name one object, or ask for a cascade")
        return self


class GiveIn(Target):
    """A gift: to whom, whether the giver's own notes go along, and the second-factor code."""

    to_user: str = Field(max_length=64)
    with_notes: bool = False
    code: str = Field(default="", max_length=30)


class TakeIn(Target):
    """Taking back: the second-factor code for accounts that have one."""

    code: str = Field(default="", max_length=30)


def _audit(conn, registry, who, event, node_id, *, commit=True, outcome="success", **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="node",
        object_id=node_id,
        outcome=outcome,
        details=details,
        commit=commit,
    )


def _listing(items: list[tuple[str, str]], limit: int = 20) -> str:
    """Name the objects of a change for the audit trail (capped; ids are not secrets)."""
    shown = ",".join(f"{kind}:{object_id}" for kind, object_id in items[:limit])
    return shown + (",…" if len(items) > limit else "")


@contextmanager
def _refusals(
    conn: sqlite3.Connection, registry: Registry, who: Principal, event: str, node_id: str
):
    """Leave a trace of an ownership action that was refused after the caller was identified.

    A flood of rate-limited codes is not recorded (it is already cut short), and neither is a
    machine the caller may not see: only what an investigator would want to know.
    """
    try:
        yield
    except ApiError as error:
        if error.code.startswith(("ownership.", "auth.invalid_code", "auth.2fa_required")):
            _audit(conn, registry, who, event, node_id, outcome="failure", code=error.code)
        raise


def _session_with_code(request: Request, conn: sqlite3.Connection, who: Principal, code: str):
    """Only the person's own session; and their fresh code when the account has a second factor."""
    if who.kind != "session":
        raise MissingRight("ownership.not_with_key")
    if second_factor.is_enabled(conn, who.id):
        second_factor.require_fresh_code(conn, request.app.state.settings, who.id, code)


def _visible_node(conn: sqlite3.Connection, registry: Registry, who: Principal, node_id: str):
    node = nodes_repo.get_node(conn, node_id)
    if node is None or not who.has("nodes.read", registry.rights(), f"node:{node_id}"):
        raise NotFound()
    return node


async def _items(
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    node: dict[str, Any],
    who: Principal,
    target: Target,
) -> list[tuple[str, str]]:
    """Return the objects a gift names, all of them owned by the caller."""
    if target.cascade:
        found = await service.owned_objects(request, conn, registry, node, who, target.only_type)
    else:
        kind, object_id = target.object_type or "", target.object_id or ""
        service.check_object(registry, kind, object_id)
        has_row = (kind, object_id) in repository.rows_of_node(conn, node["id"])
        if not has_row and node["owner"] == who.id:
            # A default object must really exist, or a gift could invent names.
            if object_id not in await service.existing_ids(request, registry, node["id"], kind):
                raise NotFound()
        found = [(kind, object_id)]
    catalogue = registry.rights()
    # Owning is not enough: the right to give must hold too (a machine suspended, say, blocks it).
    giveable = [
        item
        for item in found
        if repository.owner_of(conn, node["id"], *item) == who.id
        and who.has(service.GIVE, catalogue, service.scope_of(node["id"], *item))
    ]
    if not giveable:
        raise ApiError("ownership.nothing_to_give", 409)
    return giveable


@router.get("/nodes/{node_id}/ownership", summary="Who owns the objects of a machine")
async def list_owners(
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the kinds that can be owned and the caller's own objects.

    The machine's owner also sees who owns each object that is not theirs; others do not.
    """
    node = _visible_node(conn, registry, who, node_id)
    manager = who.has(service.MANAGE, registry.rights(), f"node:{node_id}")
    rows = repository.rows_of_node(conn, node_id)
    owners = sorted(set(rows.values()))
    names = {
        r["id"]: r["username"]
        for r in conn.execute(
            "SELECT id, username FROM users WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(owners),),
        )
    }
    return {
        "types": sorted(registry.ownables()),
        "node_owner": node["owner"] == who.id,
        "can_take": manager,
        "mine": [{"type": t, "id": i} for (t, i), o in sorted(rows.items()) if o == who.id],
        "others": [
            {"type": t, "id": i, "owner": o, "owner_name": names.get(o)}
            for (t, i), o in sorted(rows.items())
            if manager and o != who.id
        ],
    }


@router.get("/nodes/{node_id}/ownership/people", summary="Find the person to give an object to")
def find_person(
    username: str = Query(min_length=1, max_length=64),
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return the person with exactly this user name, if they can see the machine.

    An exact name and a few tries a minute; nothing for someone who cannot see the machine.
    """
    _visible_node(conn, registry, who, node_id)
    # Only someone who has something to give may look people up (not any reader of the machine).
    if who.kind != "session" or not (
        who.has(service.GIVE, registry.rights(), f"node:{node_id}")
        or repository.owned_by(conn, node_id, who.id)
    ):
        raise MissingRight()
    if people_limiter.blocked(user=who.id):
        raise ApiError("members.too_many_searches", 429)
    people_limiter.remember(user=who.id)
    row = conn.execute(
        "SELECT id, username FROM users WHERE lower(username) = lower(?) AND is_active = 1"
        " AND kind = 'person' AND id != ?",
        (username, who.id),
    ).fetchone()
    if row is None or not service.can_see_node(registry, conn, row["id"], node_id):
        return []
    return [{"id": row["id"], "username": row["username"]}]


@router.get("/nodes/{node_id}/ownership/cascade", summary="What a cascade would move")
async def preview(
    request: Request,
    mode: str = "give",
    only_type: str | None = None,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """List the objects "give all mine" or "take all" would move, before anything is confirmed."""
    node = _visible_node(conn, registry, who, node_id)
    if only_type is not None and only_type not in registry.ownables():
        raise ApiError("ownership.unknown_object", 422)
    if mode == "take":
        who.require(service.MANAGE, registry.rights(), f"node:{node_id}")
        rows = repository.rows_of_node(conn, node_id)
        items = [k for k, o in sorted(rows.items()) if o != node["owner"]]
    elif mode == "give":
        items = await service.owned_objects(request, conn, registry, node, who, only_type)
    else:
        raise ApiError("ownership.unknown_mode", 422)
    return {"items": [{"type": t, "id": i} for t, i in items if not only_type or t == only_type]}


@router.post("/nodes/{node_id}/ownership/offers", status_code=201, summary="Give objects away")
async def give(
    body: GiveIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Offer one object, or everything the caller owns on the machine, to another person.

    Nothing changes until the receiver accepts. The receiver must be an active person who can
    already see the machine; the giver must own each object and hold the right to give it.
    """
    node = _visible_node(conn, registry, who, node_id)
    with _refusals(conn, registry, who, "ownership.offer", node_id):
        _session_with_code(request, conn, who, body.code)
        if body.to_user == who.id:
            raise ApiError("ownership.give_to_self", 409)
        target = users_repo.get_user(conn, body.to_user)
        if (
            target is None
            or not target["is_active"]
            or target["kind"] != "person"
            or not service.can_see_node(registry, conn, target["id"], node_id)
        ):
            # Same answer for "no such person" and "cannot see the machine": no way to probe.
            raise NotFound()
        items = await _items(request, conn, registry, node, who, body)
        with atomic(conn):
            repository.purge_expired(conn)
            if repository.pending_from(conn, who.id) >= service.MAX_PENDING_OFFERS:
                raise ApiError("ownership.too_many_offers", 429)
            if repository.pending_to(conn, target["id"]) >= service.MAX_INBOX:
                raise ApiError("ownership.inbox_full", 429)
            # Read again under the write lock: an owner who changed meanwhile ends the offer.
            items = [i for i in items if repository.owner_of(conn, node_id, *i) == who.id]
            if not items:
                raise ApiError("ownership.nothing_to_give", 409)
            offer_id = repository.create_offer(
                conn, node_id, who.id, target["id"], items, body.with_notes
            )
            _audit(
                conn,
                registry,
                who,
                "ownership.offer",
                node_id,
                commit=False,
                to=target["id"],
                count=len(items),
                objects=_listing(items),
                notes=body.with_notes,
            )
    return {"id": offer_id, "count": len(items)}


@router.get("/ownership/offers", summary="Gifts given and received")
def list_offers(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the gifts waiting for an answer, those sent by the caller and those they received."""
    if who.kind != "session":
        raise MissingRight("ownership.not_with_key")
    repository.purge_expired(conn)
    conn.commit()
    sent, received = [], []
    for offer in repository.offers_for(conn, who.id):
        # A machine the person can no longer see is not named to them.
        shown = {**offer}
        if not service.can_see_node(registry, conn, who.id, offer["node_id"]):
            continue
        (sent if offer["from_user"] == who.id else received).append(shown)
    return {"sent": [_public(o) for o in sent], "received": [_public(o) for o in received]}


def _public(offer: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "node_id",
        "node_name",
        "from_name",
        "to_name",
        "with_notes",
        "items",
        "expires_at",
    )
    return {k: offer[k] for k in keys}


def _own_offer(conn: sqlite3.Connection, who: Principal, offer_id: str, side: str) -> dict:
    """Return a live gift the caller is the `side` ("to_user" or "from_user") of, or 404."""
    if who.kind != "session":
        raise MissingRight("ownership.not_with_key")
    offer = repository.get_offer(conn, offer_id)
    if offer is None or offer[side] != who.id:
        raise NotFound()
    return offer


@router.post("/ownership/offers/{offer_id}/accept", summary="Accept a gift")
def accept(
    offer_id: str = OfferId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Become the owner of what was offered, for each object the giver still owns and may give."""
    offer = _own_offer(conn, who, offer_id, "to_user")
    giver = users_repo.get_user(conn, offer["from_user"])
    node_id = offer["node_id"]
    with _refusals(conn, registry, who, "ownership.accept", node_id):
        if (
            nodes_repo.get_node(conn, node_id) is None
            or not service.can_see_node(registry, conn, who.id, node_id)
            or giver is None
            or not giver["is_active"]
            or not service.can_see_node(registry, conn, giver["id"], node_id)
        ):
            # The machine is gone or hidden from you, or the giver can no longer act: void.
            with atomic(conn):
                repository.delete_offer(conn, offer_id)
            raise ApiError("ownership.offer_void", 409)
        moved = skipped = 0
        done: list[tuple[str, str]] = []
        with atomic(conn):
            if not repository.delete_offer(conn, offer_id):
                raise NotFound()  # answered by someone else a moment ago
            # Read again under the lock: a machine handed over meanwhile has another owner.
            node = nodes_repo.get_node(conn, node_id)
            if node is None:
                raise NotFound()
            holder = registry.principal_for(conn, giver["id"], kind="background")
            for item in ((i["type"], i["id"]) for i in offer["items"]):
                still_theirs = repository.owner_of(conn, node_id, *item) == giver["id"]
                # The giver must still hold the right to give: a demoted member's gift lapses.
                if not still_theirs or not holder.has(
                    service.GIVE, registry.rights(), service.scope_of(node_id, *item)
                ):
                    skipped += 1
                    continue
                service.move(conn, registry, node, item, giver["id"], who.id, offer["with_notes"])
                done.append(item)
                moved += 1
            _audit(
                conn,
                registry,
                who,
                "ownership.accept",
                node_id,
                commit=False,
                giver=giver["id"],
                moved=moved,
                skipped=skipped,
                notes=offer["with_notes"],
                objects=_listing(done),
            )
    return {"moved": moved, "skipped": skipped}


@router.post("/ownership/offers/{offer_id}/refuse", status_code=204, summary="Refuse a gift")
def refuse(
    offer_id: str = OfferId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Turn a gift down. Nothing changes for the giver."""
    offer = _own_offer(conn, who, offer_id, "to_user")
    with atomic(conn):
        if repository.delete_offer(conn, offer_id):
            _audit(
                conn,
                registry,
                who,
                "ownership.refuse",
                offer["node_id"],
                commit=False,
                giver=offer["from_user"],
            )


@router.delete("/ownership/offers/{offer_id}", status_code=204, summary="Withdraw a gift")
def withdraw(
    offer_id: str = OfferId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Take back a gift that was not answered yet."""
    offer = _own_offer(conn, who, offer_id, "from_user")
    with atomic(conn):
        if repository.delete_offer(conn, offer_id):
            _audit(
                conn,
                registry,
                who,
                "ownership.withdraw",
                offer["node_id"],
                commit=False,
                to=offer["to_user"],
            )


@router.post("/nodes/{node_id}/ownership/take", summary="Take objects back")
async def take(
    body: TakeIn,
    request: Request,
    node_id: str = NodeId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Give one object, or every object someone else owns on the machine, back to its owner.

    Only the machine's owner can (`ownership.manage`). Notes are never moved. Gifts naming the
    objects are withdrawn, and the former owner's schedules and hooks on them are stopped.
    """
    authorise(conn, registry, who, node_id, service.MANAGE)
    with _refusals(conn, registry, who, "ownership.take", node_id):
        _session_with_code(request, conn, who, body.code)
        if body.cascade:
            if body.only_type is not None and body.only_type not in registry.ownables():
                raise ApiError("ownership.unknown_object", 422)
        else:
            service.check_object(registry, body.object_type or "", body.object_id or "")
        moved = 0
        taken: list[tuple[str, str]] = []
        former_owners: list[str] = []
        with atomic(conn):
            # Read again under the lock: a machine handed over meanwhile has another owner.
            node = nodes_repo.get_node(conn, node_id)
            if node is None or not registry.principal_for(conn, who.id).has(
                service.MANAGE, registry.rights(), f"node:{node_id}"
            ):
                raise NotFound()
            rows = repository.rows_of_node(conn, node_id)
            if body.cascade:
                items = [
                    k
                    for k, o in sorted(rows.items())
                    if o != node["owner"] and (not body.only_type or k[0] == body.only_type)
                ]
            else:
                items = [(body.object_type or "", body.object_id or "")]
            for item in items:
                former = rows.get(item)
                if former is None or former == node["owner"]:
                    continue  # already the machine owner's
                service.move(conn, registry, node, item, former, node["owner"], False)
                taken.append(item)
                former_owners.append(former)
                moved += 1
            service.drop_pending(conn, node_id, items)
            if moved == 0 and not body.cascade:
                raise NotFound()
            _audit(
                conn,
                registry,
                who,
                "ownership.take",
                node_id,
                commit=False,
                moved=moved,
                cascade=body.cascade,
                objects=_listing(taken),
                from_owners=",".join(sorted(set(former_owners)))[:200],
            )
    return {"moved": moved}
