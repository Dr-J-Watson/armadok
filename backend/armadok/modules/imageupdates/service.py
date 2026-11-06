"""Checking a node's images against their registries."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from armadok.core import crypto
from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.modules.imageupdates import refs, registry, repository
from armadok.modules.workers.routes import call_operation

MAX_IMAGES = 100
# Registries asked in one check: a node naming hundreds of hosts is a way to make the hub
# knock on them all.
MAX_REGISTRIES = 10
SAFE_REF = re.compile(r"[^A-Za-z0-9._:/@-]")
CONCURRENCY = 4
DEADLINE_SECONDS = 180
_busy: set[str] = set()
# Shared by every project check, so many tasks due together do not multiply the requests.
_GATE = asyncio.Semaphore(CONCURRENCY)
log = logging.getLogger("armadok.imageupdates")


class LocalImage(BaseModel):
    """One named image of the node, as its worker reports it (untrusted)."""

    model_config = ConfigDict(extra="ignore")
    ref: str = Field(max_length=255)
    digests: list[str] = Field(default_factory=list, max_length=5)
    in_use: bool = False

    @field_validator("ref")
    @classmethod
    def printable(cls, value: str) -> str:
        """Keep only characters an image name can hold: the rest becomes `?` (it is only shown)."""
        return SAFE_REF.sub("?", value)


class LocalImages(BaseModel):
    """The worker's answer: the node's named images."""

    model_config = ConfigDict(extra="ignore")
    images: list[LocalImage] = Field(max_length=300)
    truncated: bool = False


class Busy(Exception):
    """A check of this node is already running."""


async def _compare(
    image: LocalImage,
    allow_private: bool,
    gate: asyncio.Semaphore,
    logins: dict[str, registry.Login],
) -> dict[str, Any]:
    """Return the state of one image: `current`, `update`, `unknown` or `error`."""
    base: dict[str, Any] = {
        "ref": image.ref,
        "in_use": image.in_use,
        "local": [d for d in image.digests if registry.DIGEST.fullmatch(d)],
        "remote": None,
        "error": None,
    }
    parsed = refs.parse(image.ref)
    if parsed is None:
        return {**base, "state": "unknown", "error": "images.not_followed"}
    if not base["local"]:
        # Built or loaded here: there is no registry digest to compare with.
        return {**base, "state": "unknown", "error": "images.no_digest"}
    async with gate:
        try:
            remote = await registry.current_digest(parsed, allow_private, logins.get(parsed.host))
        except registry.RegistryError as error:
            return {**base, "state": "error", "error": error.code}
        except Exception:  # noqa: BLE001  (one odd registry must not sink the whole check)
            log.exception("could not compare %s", image.ref)
            return {**base, "state": "error", "error": "images.registry_unreachable"}
    state = "current" if remote in base["local"] else "update"
    return {**base, "state": state, "remote": remote}


def _open_logins(app: Any, node_id: str) -> dict[str, registry.Login]:
    """Decrypt the node's registry logins; one that cannot be read is simply not used."""
    conn = app.state.settings.open_db()
    try:
        sealed = repository.sealed_logins(conn, node_id)
    finally:
        conn.close()
    logins: dict[str, registry.Login] = {}
    for host, (username, secret) in sealed.items():
        try:
            logins[host] = registry.Login(username, crypto.decrypt(app.state.settings, secret))
        except ApiError:
            log.warning("a registry login of %s could not be decrypted", node_id)
    return logins


async def check_node(app: Any, node_id: str) -> dict[str, int]:
    """Compare the node's images in use with their registries; store and announce.

    Returns counts by state. Only images a container uses are followed (the others are
    leftovers). A new update is announced once per remote digest. Raises `Busy`.
    """
    if node_id in _busy:
        raise Busy
    _busy.add(node_id)
    try:
        raw = await call_operation(app, app.state.registry, node_id, "docker.images.digests", {})
        listing = LocalImages.model_validate(raw)
        # One entry per reference; the same name listed twice keeps the in-use one.
        chosen: dict[str, LocalImage] = {}
        for image in listing.images:
            if image.in_use and (image.ref not in chosen or not chosen[image.ref].in_use):
                chosen[image.ref] = image
        gate = asyncio.Semaphore(CONCURRENCY)
        logins = await asyncio.to_thread(_open_logins, app, node_id)
        allow_private = app.state.settings.allow_private_outbound
        followed, hosts = [], set()
        crowded = []
        for image in list(chosen.values())[:MAX_IMAGES]:
            parsed = refs.parse(image.ref)
            if parsed is not None:
                hosts.add(parsed.host)
                if len(hosts) > MAX_REGISTRIES:
                    crowded.append(image)
                    continue
            followed.append(image)
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                results = await asyncio.gather(
                    *(_compare(i, allow_private, gate, logins) for i in followed)
                )
        except TimeoutError:
            raise ApiError("imageupdates.timeout", 504) from None
        results = [
            *results,
            *(
                {
                    "ref": i.ref,
                    "in_use": i.in_use,
                    "local": [],
                    "remote": None,
                    "state": "unknown",
                    "error": "images.too_many_registries",
                }
                for i in crowded
            ),
        ]
        announce: list[dict[str, Any]] = []
        await asyncio.to_thread(_store, app, node_id, results, announce)
        for item in announce:
            await emit(
                app,
                "notify",
                event="images.update",
                node_id=node_id,
                params={"image": item["ref"]},
            )
        counts = {s: 0 for s in repository.STATES}
        for item in results:
            counts[item["state"]] += 1
        return counts
    finally:
        _busy.discard(node_id)


async def newer_exists(app: Any, node_id: str, wanted: list[str]) -> bool:
    """Say whether any of these image references (as containers name them) has a newer version.

    A quick check of a handful of images, for a project: nothing is stored and nobody is
    notified (the node-wide check does that). References that cannot be followed, or whose
    registry does not answer, count as "no news": an update is never guessed.
    """
    targets = {p for ref in wanted if (p := refs.parse(ref)) is not None}
    # Same bounds as the node-wide check: a container can name any registry.
    if len({t.host for t in targets}) > MAX_REGISTRIES or len(targets) > MAX_IMAGES:
        return False
    if not targets:
        return False
    raw = await call_operation(app, app.state.registry, node_id, "docker.images.digests", {})
    try:
        listing = LocalImages.model_validate(raw)
    except ValueError:
        raise ApiError("worker.invalid_answer", 502) from None
    mine = {i.ref: i for i in listing.images if refs.parse(i.ref) in targets}
    if not mine:
        return False
    logins = await asyncio.to_thread(_open_logins, app, node_id)
    allow_private = app.state.settings.allow_private_outbound
    try:
        async with asyncio.timeout(DEADLINE_SECONDS // 3):
            results = await asyncio.gather(
                *(_compare(i, allow_private, _GATE, logins) for i in mine.values())
            )
    except TimeoutError:
        raise ApiError("imageupdates.timeout", 504) from None
    return any(item["state"] == "update" for item in results)


def _store(app: Any, node_id: str, results: list[dict[str, Any]], announce: list) -> None:
    conn = app.state.settings.open_db()
    try:
        for item in results:
            told = repository.previous_notified(conn, node_id, item["ref"])
            notify = item["state"] == "update" and item["remote"] != told
            if notify:
                announce.append(item)
            repository.save(
                conn,
                node_id,
                item["ref"],
                state=item["state"],
                in_use=item["in_use"],
                local=item["local"],
                remote=item["remote"],
                error=item["error"],
                notified=item["remote"] if (notify or item["state"] == "update") else None,
            )
        repository.forget_others(conn, node_id, {r["ref"] for r in results})
        conn.commit()
    finally:
        conn.close()
