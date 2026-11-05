"""Reading how running containers were started, so the hub can turn them into a Compose file.

Unlike the other Docker reads, this one returns the **environment variables** of the
containers (the hub needs them to write the file). The hub only asks for it behind a sensitive
right that API keys cannot hold, and never stores or logs the values.

Only a fixed set of settings is kept, and whatever the image itself defines (its own
environment, command, labels, health check…) is left out, so the result is what the person
chose when they started the container and not the whole inspection.
"""

from __future__ import annotations

import re
from typing import Any

from armadok.worker.docker_ops import MAX_TEXT, container_id, docker_get
from armadok.worker.operation import Operation, OperationError

MAX_CONTAINERS = 20
MAX_ITEMS = 100  # most entries of any list or map
MAX_VALUE = 4000  # longest single value (an environment variable or a label)
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_DEFAULT_SHM = 67108864
# Set by Compose itself or by the image; none of them is a choice worth copying.
_SKIP_LABEL_PREFIXES = ("com.docker.compose.", "com.docker.desktop.")


def _cut(value: Any, limit: int = MAX_VALUE) -> str:
    """Return `value` as text, cut to `limit` characters."""
    return str(value)[:limit]


def _list(values: Any, limit: int = MAX_VALUE) -> list[str]:
    """Return a list of at most `MAX_ITEMS` short strings (anything else becomes empty)."""
    if not isinstance(values, list):
        return []
    return [_cut(v, limit) for v in values[:MAX_ITEMS] if isinstance(v, str)]


def _different(mine: list[str] | None, image: list[str] | None) -> list[str]:
    """Return the entries of `mine` that the image does not already define."""
    known = set(image or [])
    return [v for v in _list(mine) if v not in known]


def _ports(bindings: Any) -> list[str]:
    """Turn Docker's port bindings into `[ip:]host:container/proto` texts."""
    out: list[str] = []
    if not isinstance(bindings, dict):
        return out
    for container_port, hosts in list(bindings.items())[:MAX_ITEMS]:
        for host in (hosts or [])[:MAX_ITEMS]:
            if not isinstance(host, dict):
                continue
            ip, port = host.get("HostIp") or "", host.get("HostPort") or ""
            out.append(f"{ip + ':' if ip and ip != '0.0.0.0' else ''}{port}:{container_port}")
    return out


def _mounts(mounts: Any) -> list[dict[str, Any]]:
    """Keep type, source (or volume name), destination and whether it is writable."""
    out: list[dict[str, Any]] = []
    for m in (mounts if isinstance(mounts, list) else [])[:MAX_ITEMS]:
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "type": _cut(m.get("Type"), 20),
                "name": _cut(m.get("Name") or "", MAX_TEXT),
                "source": _cut(m.get("Source") or "", 1000),
                "destination": _cut(m.get("Destination") or "", 1000),
                "rw": bool(m.get("RW", True)),
            }
        )
    return out


def _healthcheck(mine: Any, image: Any) -> dict[str, Any] | None:
    """Return the container's own health check, or None if it is the image's or absent."""
    if not isinstance(mine, dict) or mine == image or not mine.get("Test"):
        return None
    return {
        "test": _list(mine.get("Test")),
        "interval": int(mine.get("Interval") or 0),
        "timeout": int(mine.get("Timeout") or 0),
        "retries": int(mine.get("Retries") or 0),
        "start_period": int(mine.get("StartPeriod") or 0),
    }


def _devices(devices: Any) -> list[str]:
    """Turn device mappings into `host:container:permissions` texts."""
    out = []
    for d in (devices if isinstance(devices, list) else [])[:MAX_ITEMS]:
        if isinstance(d, dict):
            out.append(
                f"{_cut(d.get('PathOnHost'), 300)}:{_cut(d.get('PathInContainer'), 300)}"
                f":{_cut(d.get('CgroupPermissions') or 'rwm', 10)}"
            )
    return out


def _was_cut(config: dict[str, Any], host: dict[str, Any], box: dict[str, Any]) -> bool:
    """Say whether a list or a value was too long to be kept whole (the hub then warns)."""
    lists = (config.get("Env"), box.get("Mounts"), host.get("Binds"), config.get("Labels"))
    if any(isinstance(v, (list, dict)) and len(v) > MAX_ITEMS for v in lists):
        return True
    return any(isinstance(v, str) and len(v) > MAX_VALUE for v in config.get("Env") or [])


def describe(box: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    """Reduce one container's inspection (and its image's) to the settings worth converting."""
    config, host = box.get("Config") or {}, box.get("HostConfig") or {}
    base = image.get("Config") or {}
    labels = {
        _cut(k, 200): _cut(v)
        for k, v in list((config.get("Labels") or {}).items())[:MAX_ITEMS]
        if not str(k).startswith(_SKIP_LABEL_PREFIXES) and (base.get("Labels") or {}).get(k) != v
    }
    restart = host.get("RestartPolicy") or {}
    mode = _cut(host.get("NetworkMode") or "", MAX_TEXT)
    networks = sorted((box.get("NetworkSettings") or {}).get("Networks") or {})[:MAX_ITEMS]
    log = host.get("LogConfig") or {}
    name = _cut(box.get("Name") or "", MAX_TEXT).lstrip("/")
    return {
        "truncated": _was_cut(config, host, box),
        "name": name,
        "image": _cut(config.get("Image") or "", MAX_TEXT),
        "compose_project": _cut(
            (config.get("Labels") or {}).get("com.docker.compose.project") or "", 100
        ),
        "env": _different(config.get("Env"), base.get("Env")),
        "command": [] if config.get("Cmd") == base.get("Cmd") else _list(config.get("Cmd")),
        "entrypoint": []
        if config.get("Entrypoint") == base.get("Entrypoint")
        else _list(config.get("Entrypoint")),
        "working_dir": ""
        if config.get("WorkingDir") == base.get("WorkingDir")
        else _cut(config.get("WorkingDir") or "", 1000),
        "user": "" if config.get("User") == base.get("User") else _cut(config.get("User") or ""),
        "hostname": ""
        if config.get("Hostname") in ("", str(box.get("Id", ""))[:12])
        else _cut(config.get("Hostname"), MAX_TEXT),
        "labels": labels,
        "ports": _ports(host.get("PortBindings")),
        "mounts": _mounts(box.get("Mounts")),
        "tmpfs": sorted(host.get("Tmpfs") or {})[:MAX_ITEMS],
        "restart": _cut(restart.get("Name") or "", 30),
        "restart_retries": int(restart.get("MaximumRetryCount") or 0),
        "network_mode": mode,
        "networks": networks,
        "privileged": bool(host.get("Privileged")),
        "cap_add": _list(host.get("CapAdd"), 60),
        "cap_drop": _list(host.get("CapDrop"), 60),
        "devices": _devices(host.get("Devices")),
        "dns": _list(host.get("Dns"), 100),
        "extra_hosts": _list(host.get("ExtraHosts"), 300),
        "security_opt": _list(host.get("SecurityOpt"), 300),
        "group_add": _list(host.get("GroupAdd"), 100),
        "memory": int(host.get("Memory") or 0),
        "nano_cpus": int(host.get("NanoCpus") or 0),
        "read_only": bool(host.get("ReadonlyRootfs")),
        "init": bool(host.get("Init")),
        "tty": bool(config.get("Tty")),
        "stdin_open": bool(config.get("OpenStdin")),
        "log_driver": "" if log.get("Type") in (None, "json-file") else _cut(log.get("Type"), 60),
        "sysctls": {
            _cut(k, 200): _cut(v, 200)
            for k, v in list((host.get("Sysctls") or {}).items())[:MAX_ITEMS]
        },
        "pid_mode": _cut(host.get("PidMode") or "", 60),
        "shm_size": 0 if host.get("ShmSize") in (None, _DEFAULT_SHM) else int(host["ShmSize"]),
        "stop_signal": ""
        if config.get("StopSignal") == base.get("StopSignal")
        else _cut(config.get("StopSignal") or "", 30),
        "stop_timeout": int(config.get("StopTimeout") or 0),
        "healthcheck": _healthcheck(config.get("Healthcheck"), base.get("Healthcheck")),
    }


def containers_inspect(args: dict[str, Any]) -> dict[str, Any]:
    """Describe up to `MAX_CONTAINERS` containers by id (`{"ids": [...]}`), in the order asked."""
    ids = args.get("ids")
    if not isinstance(ids, list) or not 0 < len(ids) <= MAX_CONTAINERS:
        raise OperationError("worker.invalid_path")
    described = []
    for raw in ids:
        box = docker_get(f"/containers/{container_id(raw)}/json")
        image_id = str(box.get("Image") or "")
        if not _IMAGE_ID.fullmatch(image_id):
            image_id = ""  # never put an odd text in a request path
        try:
            image = docker_get(f"/images/{image_id}/json") if image_id else {}
        except OperationError:
            image = {}  # the image is gone: everything then counts as the person's choice
        described.append(describe(box, image))
    return {"containers": described}


OPERATIONS: dict[str, Operation] = {"docker.containers.inspect": containers_inspect}
