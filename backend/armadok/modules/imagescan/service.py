"""Scanning an image: list its packages on the node, ask OSV, keep what is found."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from armadok.core.errors import ApiError
from armadok.core.events import emit
from armadok.modules.docker import schemas as docker_schemas
from armadok.modules.imagescan import repository
from armadok.modules.vulns import osv
from armadok.modules.workers.routes import call_operation

MAX_IDS = 4000  # a full Debian image can hold far more flaws than Armadok's own packages
DEADLINE_SECONDS = 600
ALERT = ("critical", "high", "unknown")
MAX_IMAGES_PER_RUN = 20
SAFE_NAME = re.compile(r"[^A-Za-z0-9._:/@-]")
IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_busy: set[str] = set()


class Package(BaseModel):
    """One installed package as the worker reports it (untrusted)."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,127}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,127}$")


class Packages(BaseModel):
    """The worker's answer: the operating system and its packages."""

    model_config = ConfigDict(extra="ignore")
    os: str = Field(pattern=r"^[a-z0-9._-]{0,32}$")
    os_version: str = Field(pattern=r"^[0-9A-Za-z._-]{0,32}$")
    truncated: bool = False
    packages: list[Package] = Field(max_length=5000)


def ecosystem(os_id: str, version: str) -> str | None:
    """Return OSV's name for the operating system, or None if it is not one we can ask about."""
    if os_id == "debian" and re.fullmatch(r"[0-9]{1,2}", version):
        return f"Debian:{version}"
    if os_id == "ubuntu" and re.fullmatch(r"[0-9]{2}\.[0-9]{2}", version):
        return f"Ubuntu:{version}"
    if os_id == "alpine":
        parts = version.split(".")
        if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]):
            return f"Alpine:v{parts[0]}.{parts[1]}"
    return None


async def scan_image(app: Any, node_id: str, image_id: str, name: str) -> dict[str, Any]:
    """Scan one image of a node and store the result; return what was stored.

    The result's `status` is `ok`, `unsupported` (an operating system OSV does not cover),
    `empty` (no package list found) or an error code.
    """
    if not IMAGE.fullmatch(image_id):
        raise ApiError("imagescan.invalid_image", 422)
    key = f"{node_id}:{image_id}"
    if key in _busy:
        raise ApiError("imagescan.busy", 409)
    _busy.add(key)
    try:
        raw = await call_operation(
            app, app.state.registry, node_id, "images.packages", {"image": image_id}
        )
        try:
            listed = Packages.model_validate(raw)
        except ValidationError:
            raise ApiError("worker.invalid_answer", 502) from None
        systems = ecosystem(listed.os, listed.os_version)
        findings: list[dict[str, Any]] = []
        if not listed.packages:
            status = "empty"
        elif systems is None:
            status = "unsupported"
        else:
            status = "ok"
            try:
                found, _ = await osv.scan(
                    [(p.name, p.version) for p in listed.packages],
                    systems,
                    max_ids=MAX_IDS,
                    deadline=DEADLINE_SECONDS,
                )
                findings = found
                if listed.truncated:
                    status = "partial"
            except osv.OsvError as error:
                status = str(error)
        before = await asyncio.to_thread(_previous_ids, app, node_id, image_id)
        await asyncio.to_thread(
            _save, app, node_id, image_id, name, listed, len(listed.packages), status, findings
        )
        new = [f for f in findings if f["severity"] in ALERT and f["id"] not in before]
        if new:
            await emit(
                app,
                "notify",
                event="imagescan.found",
                node_id=node_id,
                params={"image": name, "count": len(new)},
            )
        return {"status": status, "findings": len(findings)}
    finally:
        _busy.discard(key)


def _forget(app: Any, node_id: str, present: set[str]) -> None:
    conn = app.state.settings.open_db()
    try:
        repository.forget_others(conn, node_id, present)
    finally:
        conn.close()


def _previous_ids(app: Any, node_id: str, image_id: str) -> set[str]:
    conn = app.state.settings.open_db()
    try:
        row = repository.get(conn, node_id, image_id)
        return {f["id"] for f in repository.view(row)["findings"]} if row else set()
    finally:
        conn.close()


def _save(app, node_id, image_id, name, listed, count, status, findings) -> None:
    conn = app.state.settings.open_db()
    try:
        repository.store(
            conn,
            node_id,
            image_id,
            name,
            os_id=listed.os,
            os_version=listed.os_version,
            packages=count,
            status=status,
            findings=findings,
        )
    finally:
        conn.close()


async def _listings(app: Any, node_id: str) -> tuple[Any, Any]:
    """Ask the node for its containers and images, and check the (untrusted) answers."""
    registry = app.state.registry
    try:
        containers = docker_schemas.ContainerList.model_validate(
            await call_operation(app, registry, node_id, "docker.containers.list", {})
        )
        images = docker_schemas.Images.model_validate(
            await call_operation(app, registry, node_id, "docker.images.list", {})
        )
    except ValidationError:
        raise ApiError("worker.invalid_answer", 502) from None
    return containers, images


async def images_in_use(app: Any, node_id: str) -> list[tuple[str, str]]:
    """Return `(image id, name)` of the images the node's containers run, at most 20."""
    containers, images = await _listings(app, node_id)
    await asyncio.to_thread(_forget, app, node_id, {i.id for i in images.items})
    wanted = {c.image for c in containers.containers if c.image}
    found = []
    for image in images.items:
        if not IMAGE.fullmatch(image.id):
            continue
        if wanted & set(image.tags) or any(image.id.startswith(f"sha256:{w}") for w in wanted):
            found.append((image.id, _name(image)))
    return found[:MAX_IMAGES_PER_RUN]


async def name_of(app: Any, node_id: str, image_id: str) -> str | None:
    """Return the name of an image the node has, or None if it has no such image."""
    _, images = await _listings(app, node_id)
    for image in images.items:
        if image.id == image_id:
            return _name(image)
    return None


def _name(image: Any) -> str:
    """Return a plain-character name for an image (its first tag, else a short id)."""
    return SAFE_NAME.sub("?", image.tags[0] if image.tags else image.id[7:19])[:120]
