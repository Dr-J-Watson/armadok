"""DNS endpoints: provider accounts, domains and records."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

from armadok.core import audit, crypto
from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.errors import ApiError, MissingRight, NotFound
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.dns import base, repository, service
from armadok.modules.nodes import repository as nodes_repo

router = APIRouter(prefix="/dns", tags=["dns"])

# A record id is whatever the provider uses; this only keeps it a harmless token.
RecordId = Path(pattern=r"^[A-Za-z0-9:._-]{1,100}$")
MAX_AUDITED_VALUE = 200


class AccountIn(BaseModel):
    """A provider account: its kind, a name for yourself and the provider's credentials."""

    provider: str = Field(max_length=30)
    name: str = Field(min_length=1, max_length=60)
    credentials: dict[str, Any] = Field(default_factory=dict, max_length=10)


class DomainIn(BaseModel):
    """A domain to add, with the account that holds its zone (none = local DNS)."""

    name: str = Field(max_length=253)
    account_id: str | None = Field(default=None, max_length=64)


class ImportIn(BaseModel):
    """Zones of an account to add as domains."""

    zones: list[str] = Field(min_length=1, max_length=repository.MAX_DOMAINS)


class AttachIn(BaseModel):
    """The node to attach a domain to (null detaches it), and what its members may write.

    Members use YOUR provider account, so they get a narrow default: address and alias records
    below the domain's apex only, never mail, name-server or certificate-validation records.
    """

    node_id: str | None = Field(max_length=64)
    member_types: list[str] = Field(default_factory=lambda: ["A", "AAAA", "CNAME"], max_length=8)
    member_apex: bool = False
    member_names: list[str] = Field(default_factory=list, max_length=20)


class RecordIn(BaseModel):
    """A record to create or replace. `name` is relative to the domain ("" = apex)."""

    type: str = Field(pattern="^(" + "|".join(base.RECORD_TYPES) + ")$")
    name: str = Field(default="", max_length=253, pattern=base.SUBDOMAIN_PATTERN)
    target: str = Field(min_length=1, max_length=2000, pattern=r"^[^\x00-\x1f\x7f]+$")
    ttl: int = Field(default=3600, ge=60, le=604800)

    @model_validator(mode="after")
    def target_fits_type(self) -> RecordIn:
        """Check that the value fits the type (A: IPv4, AAAA: IPv6, CNAME: a host name)."""
        if not base.valid_target(self.type, self.target):
            raise ValueError("target does not fit the record type")
        return self


def _clip(value: Any) -> Any:
    return value[:MAX_AUDITED_VALUE] if isinstance(value, str) else value


def _audit(conn, registry, who, event, outcome="success", **details):
    audit.record(
        conn,
        registry.audit_events(),
        who.actor,
        event,
        object_type="dns",
        outcome=outcome,
        details={k: _clip(v) for k, v in details.items()},
    )


def _use(registry: Registry, who: Principal) -> None:
    """Every DNS route needs `dns.use`: an API key only gets DNS if it was given that right."""
    who.require("dns.use", registry.rights())


def _domain_for(
    conn: sqlite3.Connection, registry: Registry, who: Principal, domain_id: str, need: str
) -> tuple[sqlite3.Row, str]:
    """Return `(domain, access)`; 404 if invisible, 403 if `need` ("read" or "write") is not met."""
    domain = repository.get_domain(conn, domain_id)
    level = service.access(registry, conn, who, domain) if domain else None
    if level is None:
        raise NotFound()
    if need == "write" and level == "read":
        raise MissingRight()
    return domain, level


# --- providers and accounts ---------------------------------------------------------------


@router.get("/providers", summary="The DNS providers Armadok supports")
def providers(registry: Registry = Depends(get_registry), who: Principal = Depends(get_principal)):
    """Return each provider with what it can do and the credential fields it needs."""
    _use(registry, who)
    return [
        {
            "name": p.name,
            "capabilities": sorted(p.capabilities),
            "fields": sorted(p.impl.credentials.model_fields),
        }
        for p in registry.providers("dns").values()
    ]


@router.get("/accounts", summary="Your provider accounts")
def accounts(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
):
    """Return the caller's accounts. Their credentials are never sent back."""
    _use(registry, who)
    return repository.list_accounts(conn, who.id)


@router.post("/accounts", status_code=201, summary="Add a provider account")
async def add_account(
    body: AccountIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Add an account after checking its credentials work. They are stored encrypted."""
    _use(registry, who)
    if len(repository.list_accounts(conn, who.id)) >= repository.MAX_ACCOUNTS:
        raise ApiError("dns.too_many_accounts", 409)
    entry = service.provider_class(registry, body.provider)
    try:
        credentials = entry.impl.credentials.model_validate(body.credentials)
    except ValidationError:
        raise ApiError("dns.invalid_credentials", 422) from None
    try:
        await entry.impl(credentials).verify()
    except base.ProviderError as error:
        raise service.provider_error(error) from None
    sealed = crypto.encrypt(request.app.state.settings, json.dumps(credentials.model_dump()))
    account_id = repository.create_account(conn, who.id, body.provider, body.name, sealed)
    _audit(conn, registry, who, "dns.account_create", provider=body.provider, name=body.name)
    return {"id": account_id}


def _own_account(conn: sqlite3.Connection, who: Principal, account_id: str) -> sqlite3.Row:
    account = repository.get_account(conn, account_id)
    if account is None or account["owner_id"] != who.id:
        raise NotFound()
    return account


@router.delete("/accounts/{account_id}", status_code=204, summary="Remove a provider account")
def remove_account(
    account_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove an account that no domain uses any more."""
    _use(registry, who)
    _own_account(conn, who, account_id)
    if repository.domains_using(conn, account_id):
        raise ApiError("dns.account_in_use", 409)
    repository.delete_account(conn, who.id, account_id)
    _audit(conn, registry, who, "dns.account_delete")


@router.get("/accounts/{account_id}/zones", summary="Zones of an account")
async def account_zones(
    account_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[str]:
    """Return the zones the account owns (for providers that can say), to import as domains."""
    _use(registry, who)
    account = _own_account(conn, who, account_id)
    try:
        provider = service.open_provider(
            request.app,
            registry,
            conn,
            {"account_id": account_id, "owner_id": account["owner_id"]},  # type: ignore[arg-type]
        )
        return await provider.list_zones()
    except base.ProviderError as error:
        raise service.provider_error(error) from None


# --- domains ------------------------------------------------------------------------------


def _domain_json(registry, conn, who, row) -> dict:
    level = service.access(registry, conn, who, row)
    owner = level == "owner"
    account = (
        repository.get_account(conn, row["account_id"]) if row["account_id"] and owner else None
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "node_id": row["node_id"],
        "access": level,
        "capabilities": service.capabilities(registry, conn, row),
        "verified": service.verified(registry, conn, row),
        "account_name": account["name"] if account else None,
        "provider": account["provider"]
        if account
        else ("local" if row["account_id"] is None else None),
        # What members may write with the owner's account (shown to the owner).
        "member_types": row["member_types"].split(",") if owner else None,
        "member_apex": bool(row["member_apex"]) if owner else None,
        "member_names": [n for n in row["member_names"].split(",") if n] if owner else None,
    }


@router.get("/domains", summary="Domains you can see")
def domains(
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> list[dict]:
    """Return your own domains and those attached to nodes where you hold `dns.read`."""
    _use(registry, who)
    return [
        _domain_json(registry, conn, who, row)
        for row in repository.list_domains(conn)
        if service.access(registry, conn, who, row)
    ]


@router.post("/domains", status_code=201, summary="Add a domain")
async def add_domain(
    body: DomainIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Add a domain you own: backed by one of your accounts, or local (no account)."""
    _use(registry, who)
    name = body.name.strip().lower().rstrip(".")
    if not base.is_domain(name):
        raise ApiError("dns.invalid_domain", 422)
    if repository.count_domains(conn, who.id) >= repository.MAX_DOMAINS:
        raise ApiError("dns.too_many_domains", 409)
    if body.account_id is not None:
        account = _own_account(conn, who, body.account_id)
        entry = service.provider_class(registry, account["provider"])
        if "zones" in entry.capabilities:
            try:
                provider = service.open_provider(
                    request.app,
                    registry,
                    conn,
                    {"account_id": body.account_id, "owner_id": who.id},  # type: ignore[arg-type]
                )
                if name not in await provider.list_zones():
                    raise ApiError("dns.zone_not_in_account", 422)
            except base.ProviderError as error:
                raise service.provider_error(error) from None
    domain_id = repository.create_domain(conn, who.id, body.account_id, name)
    if domain_id is None:
        raise ApiError("dns.domain_exists", 409)
    _audit(conn, registry, who, "dns.domain_create", name=name)
    return {"id": domain_id}


@router.post("/accounts/{account_id}/import", status_code=201, summary="Import zones as domains")
async def import_zones(
    account_id: str,
    body: ImportIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Add the chosen zones of an account as domains (those it really owns), within the quota."""
    _use(registry, who)
    _own_account(conn, who, account_id)
    try:
        provider = service.open_provider(
            request.app,
            registry,
            conn,
            {"account_id": account_id, "owner_id": who.id},  # type: ignore[arg-type]
        )
        owned = set(await provider.list_zones())
    except base.ProviderError as error:
        raise service.provider_error(error) from None
    added = []
    for zone in body.zones:
        if zone not in owned or not base.is_domain(zone):
            continue
        if repository.count_domains(conn, who.id) >= repository.MAX_DOMAINS:
            raise ApiError("dns.too_many_domains", 409)
        if repository.create_domain(conn, who.id, account_id, zone):
            added.append(zone)
    for name in added:
        _audit(conn, registry, who, "dns.domain_create", name=name)
    return {"added": added}


@router.patch("/domains/{domain_id}", status_code=204, summary="Attach a domain to a node")
def attach_domain(
    domain_id: str,
    body: AttachIn,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Attach (or detach) one of your domains: that node's members then see it.

    Members with `dns.write` on the node can change records with YOUR provider account, within
    the limits set here (by default: A, AAAA and CNAME records below the apex only).
    """
    _use(registry, who)
    domain = repository.get_domain(conn, domain_id)
    if domain is None or domain["owner_id"] != who.id:
        raise NotFound()
    if body.node_id is not None and not (
        nodes_repo.get_node(conn, body.node_id) is not None
        and who.has("nodes.read", registry.rights(), f"node:{body.node_id}")
    ):
        raise NotFound()
    types = [t for t in body.member_types if t in base.RECORD_TYPES]
    names = [n for n in body.member_names if n]
    if any(not re.fullmatch(base.SUBDOMAIN_PATTERN, n) for n in names):
        raise ApiError("dns.invalid_restriction", 422)
    repository.attach(
        conn, domain_id, body.node_id, ",".join(types), body.member_apex, ",".join(names)
    )
    _audit(
        conn,
        registry,
        who,
        "dns.domain_attach",
        name=domain["name"],
        node=body.node_id,
        types=",".join(types),
        apex=body.member_apex,
    )


@router.delete("/domains/{domain_id}", status_code=204, summary="Remove a domain")
def remove_domain(
    domain_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Remove one of your domains from Armadok (the zone at the provider is not touched)."""
    _use(registry, who)
    domain = repository.get_domain(conn, domain_id)
    if domain is None or domain["owner_id"] != who.id:
        raise NotFound()
    repository.delete_domain(conn, domain_id)
    _audit(conn, registry, who, "dns.domain_delete", name=domain["name"])


# --- records ------------------------------------------------------------------------------


def _record_json(r: base.Record) -> dict:
    return {"id": r.id, "type": r.type, "name": r.name, "target": r.target, "ttl": r.ttl}


@router.get("/domains/{domain_id}/records", summary="Records of a domain")
async def records(
    domain_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Return the records the provider can list (empty for local DNS)."""
    domain, _ = _domain_for(conn, registry, who, domain_id, "read")
    caps = service.capabilities(registry, conn, domain)
    if "records" not in caps:
        return {"records": [], "capabilities": caps}
    try:
        provider = service.open_provider(request.app, registry, conn, domain)
        listed = await provider.list_records(domain["name"])
    except base.ProviderError as error:
        raise service.provider_error(error) from None
    return {"records": [_record_json(r) for r in listed], "capabilities": caps}


async def change_record(
    request: Request,
    conn: sqlite3.Connection,
    registry: Registry,
    who: Principal,
    domain_id: str,
    event: str,
    *,
    record_id: str | None = None,
    new: RecordIn | None = None,
) -> base.Record | None:
    """Create, replace or delete a record, with every check in one place.

    Checks: the caller may write to the domain; the provider can write; a member (not the
    owner) stays within what the owner allowed, for the OLD record as well as the new one; a
    record's type never changes. Every attempt, allowed or not, is audited with what it was
    about (names and values, never credentials).
    """
    try:
        domain, level = _domain_for(conn, registry, who, domain_id, "write")
    except (NotFound, MissingRight) as error:
        _audit(conn, registry, who, event, outcome="denied", domain_id=domain_id, record=record_id)
        raise error
    details = {
        "domain": domain["name"],
        "record": record_id,
        "type": new.type if new else None,
        "name": new.name if new else None,
        "target": new.target if new else None,
        "ttl": new.ttl if new else None,
    }
    caps = set(service.capabilities(registry, conn, domain))
    try:
        if not {"write", "publish_ip"} & caps:
            raise ApiError("dns.not_supported", 409)
        provider = service.open_provider(request.app, registry, conn, domain)
        old = None
        if record_id is not None:
            old = await provider.get_record(domain["name"], record_id)
            if old is None:
                raise ApiError("dns.not_found", 404)
            details.update(old_type=old.type, old_name=old.name, old_target=old.target)
            if new is not None and new.type != old.type:
                raise ApiError("dns.type_cannot_change", 422)
        if level != "owner":
            touched = [r for r in (old, new) if r is not None]
            if not all(service.member_may(domain, r.type, r.name) for r in touched):
                raise MissingRight()
        record = (
            base.Record(record_id or "", new.type, new.name, new.target, new.ttl) if new else None
        )
        if event == "dns.record_create":
            result = await provider.create_record(domain["name"], record)
        elif event == "dns.record_update":
            result = await provider.update_record(domain["name"], record_id, record)
        else:
            await provider.delete_record(domain["name"], record_id)
            result = None
    except base.ProviderError as error:
        _audit(conn, registry, who, event, outcome="failure", code=error.code, **details)
        raise service.provider_error(error) from None
    except (ApiError, MissingRight) as error:
        _audit(
            conn, registry, who, event, outcome="denied", code=getattr(error, "code", ""), **details
        )
        raise
    except Exception:
        _audit(conn, registry, who, event, outcome="failure", code="dns.failed", **details)
        raise ApiError("dns.failed", 502) from None
    request.app.state.__dict__.get("dns_cache", {}).pop(domain["id"], None)
    _audit(conn, registry, who, event, **details)
    return result


@router.post("/domains/{domain_id}/records", status_code=201, summary="Create a record")
async def create_record(
    domain_id: str,
    body: RecordIn,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Create a record. Needs write access to the domain; the change is audited."""
    created = await change_record(
        request, conn, registry, who, domain_id, "dns.record_create", new=body
    )
    return _record_json(created)  # type: ignore[arg-type]


@router.put("/domains/{domain_id}/records/{record_id}", summary="Change a record")
async def update_record(
    domain_id: str,
    body: RecordIn,
    request: Request,
    record_id: str = RecordId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> dict:
    """Replace a record. Its type cannot change."""
    updated = await change_record(
        request, conn, registry, who, domain_id, "dns.record_update", record_id=record_id, new=body
    )
    return _record_json(updated)  # type: ignore[arg-type]


@router.delete(
    "/domains/{domain_id}/records/{record_id}", status_code=204, summary="Delete a record"
)
async def delete_record(
    domain_id: str,
    request: Request,
    record_id: str = RecordId,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> None:
    """Delete a record. Needs write access to the domain; the change is audited."""
    await change_record(
        request, conn, registry, who, domain_id, "dns.record_delete", record_id=record_id
    )
