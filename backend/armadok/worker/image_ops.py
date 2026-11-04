"""Reading the package list of an image, without ever running it.

To look for known flaws in an image the hub needs to know which packages it holds. The worker
creates a container from the image **without starting it** (so none of the image's code runs),
copies out the few small files that list the packages (`os-release`, Debian's `dpkg/status`,
Alpine's `apk/db/installed`) through the daemon's archive call, and removes the container. The
container has no network and carries the `armadok.protect` label while it exists.

Only names and versions leave this module; anything else in the files is dropped. Each file is
size-capped, and an image with no package list we understand answers with an empty list.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import tarfile
import time
from typing import Any
from urllib.parse import quote

from armadok.worker.docker_control import PROTECT_LABEL
from armadok.worker.docker_ops import ResponseTooLarge, docker_raw
from armadok.worker.operation import Operation, OperationError

IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{0,127}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,127}")
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_PACKAGES = 5000
TIMEOUT = 30
_OS_FILES = ("/etc/os-release", "/usr/lib/os-release")
_PACKAGE_FILES = ("/var/lib/dpkg/status", "/lib/apk/db/installed", "/usr/lib/apk/db/installed")


SWEEP_AFTER_SECONDS = 300  # a scan container older than this is a leftover of a crash


def _remove(container: str) -> None:
    """Remove a scan container and its anonymous volumes; never raise (the result matters more)."""
    try:
        docker_raw("DELETE", f"/containers/{container}?force=1&v=1", TIMEOUT)
    except OperationError:
        pass


def _sweep() -> None:
    """Remove scan containers a crash left behind (the label keeps `prune` away from them)."""
    filters = quote(json.dumps({"label": ["armadok.helper=scan"]}))
    try:
        status, body = docker_raw("GET", f"/containers/json?all=1&filters={filters}", TIMEOUT)
        listed = json.loads(body) if status == 200 else []
    except (OperationError, ValueError):
        return
    now = time.time()
    for item in listed if isinstance(listed, list) else []:
        ident, created = (item.get("Id"), item.get("Created")) if isinstance(item, dict) else (0, 0)
        if (
            isinstance(ident, str)
            and re.fullmatch(r"[0-9a-f]{64}", ident)
            and type(created) is int
            and now - created > SWEEP_AFTER_SECONDS
        ):
            _remove(ident)


def _fetch(container: str, path: str) -> bytes | None:
    """Copy one file out of the (stopped) container; None if it is missing or too big."""
    try:
        status, body = docker_raw(
            "GET", f"/containers/{container}/archive?path={quote(path)}", TIMEOUT
        )
    except ResponseTooLarge:
        return None
    if status != 200:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.size <= MAX_FILE_BYTES:
                    extracted = tar.extractfile(member)
                    return extracted.read(MAX_FILE_BYTES + 1) if extracted else None
    except (tarfile.TarError, OSError, EOFError):
        return None
    return None


def parse_os(text: str) -> tuple[str, str]:
    """Return `(id, version_id)` from an os-release file (lowercase, plain characters only)."""
    values: dict[str, str] = {}
    for line in text.splitlines()[:100]:
        key, sep, value = line.partition("=")
        if sep and key in ("ID", "VERSION_ID"):
            values[key] = value.strip().strip("\"'")[:32]
    ident, version = values.get("ID", "").lower(), values.get("VERSION_ID", "")
    return (
        ident if re.fullmatch(r"[a-z0-9._-]{1,32}", ident) else "",
        version if re.fullmatch(r"[0-9][0-9A-Za-z._-]{0,31}", version) else "",
    )


def parse_dpkg(text: str) -> list[tuple[str, str]]:
    """Return `(source package, source version)` of every installed Debian package.

    Vulnerability databases speak of source packages, so `Source: openssl (3.0.11-1)` wins over
    the binary name and version when a package has one.
    """
    found: dict[tuple[str, str], None] = {}
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key in ("Package", "Status", "Version", "Source"):
                fields[key] = value.strip()
        if "installed" not in fields.get("Status", "").split()[-1:]:
            continue
        name, version = fields.get("Package", ""), fields.get("Version", "")
        source = fields.get("Source")
        if source:
            match = re.fullmatch(r"(\S+)(?: \((\S+)\))?", source)
            if match:
                name, version = match.group(1), match.group(2) or version
        if NAME.fullmatch(name) and VERSION.fullmatch(version):
            found[(name, version)] = None
    return list(found)[:MAX_PACKAGES]


def parse_apk(text: str) -> list[tuple[str, str]]:
    """Return `(origin package, version)` of every installed Alpine package."""
    found: dict[tuple[str, str], None] = {}
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep and key in ("P", "V", "o"):
                fields[key] = value.strip()
        name, version = fields.get("o") or fields.get("P", ""), fields.get("V", "")
        if NAME.fullmatch(name) and VERSION.fullmatch(version):
            found[(name, version)] = None
    return list(found)[:MAX_PACKAGES]


def image_packages(args: dict[str, Any]) -> dict[str, Any]:
    """List the packages of an image: `{os, os_version, packages: [{name, version}]}`.

    Input: `image`, a full image id (`sha256:` and 64 hex digits). The image is never started.
    Errors: `worker.invalid_path` (bad id), `worker.not_found` (no such image),
    `worker.docker_error`.
    """
    image = args.get("image")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise OperationError("worker.invalid_path")
    _sweep()
    body = {
        "Image": image,
        "Entrypoint": ["/nonexistent"],
        "Cmd": [],
        "NetworkDisabled": True,
        "Labels": {PROTECT_LABEL: "true", "armadok.helper": "scan"},
        "HostConfig": {"NetworkMode": "none", "AutoRemove": False},
    }
    status, answer = docker_raw(
        "POST", f"/containers/create?name=armadok-scan-{secrets.token_hex(6)}", TIMEOUT, body
    )
    if status == 404:
        raise OperationError("worker.not_found")
    if status != 201:
        raise OperationError("worker.docker_error")
    try:
        container = json.loads(answer).get("Id", "")
    except (ValueError, AttributeError):
        container = ""
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise OperationError("worker.docker_error")
    try:
        os_text = next((t for p in _OS_FILES if (t := _fetch(container, p))), b"")
        ident, version = parse_os(os_text.decode("utf-8", "replace"))
        packages: list[tuple[str, str]] = []
        for path in _PACKAGE_FILES:
            data = _fetch(container, path)
            if data is None:
                continue
            text = data.decode("utf-8", "replace")
            packages = parse_dpkg(text) if "dpkg" in path else parse_apk(text)
            break
    finally:
        _remove(container)
    return {
        "os": ident,
        "os_version": version,
        "packages": [{"name": n, "version": v} for n, v in packages],
        # More packages than are kept: the answer is incomplete and must not look clean.
        "truncated": len(packages) >= MAX_PACKAGES,
    }


OPERATIONS: dict[str, Operation] = {"images.packages": image_packages}
