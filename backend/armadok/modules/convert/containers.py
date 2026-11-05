"""Turning running containers into a Compose file.

The worker answers (untrusted) are parsed into strict models, then each container becomes a
service. Values that look like secrets go to a `.env` text so the Compose file can be shared;
a `$` in any value is doubled, because Compose would otherwise read it as a variable. What the
image defines by itself was already left out by the worker. Pure text in, text out: nothing is
written anywhere here.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from armadok.modules.convert.dockerrun import _SECRET, Result, env_line

Text = Annotated[str, StringConstraints(max_length=4000)]
_STRICT = ConfigDict(extra="ignore", strict=True)
_SERVICE = re.compile(r"[^a-z0-9_.-]+")
_BRIDGES = {"", "default", "bridge"}
_ANONYMOUS = re.compile(r"[0-9a-f]{64}")
_NS = 1_000_000_000
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,200}", re.ASCII)


class Mount(BaseModel):
    """One mount as the worker saw it."""

    model_config = _STRICT
    type: str = Field(max_length=20)
    name: str = Field(default="", max_length=300)
    source: str = Field(default="", max_length=1000)
    destination: str = Field(max_length=1000)
    rw: bool = True


class Health(BaseModel):
    """A health check, durations in nanoseconds."""

    model_config = _STRICT
    test: list[Text] = Field(max_length=100)
    interval: int = Field(ge=0)
    timeout: int = Field(ge=0)
    retries: int = Field(ge=0)
    start_period: int = Field(ge=0)


class Described(BaseModel):
    """One container, reduced by the worker to the settings that can be converted."""

    model_config = _STRICT
    name: str = Field(max_length=300)
    image: str = Field(max_length=300)
    compose_project: str = Field(default="", max_length=100)
    env: list[Text] = Field(default_factory=list, max_length=100)
    command: list[Text] = Field(default_factory=list, max_length=100)
    entrypoint: list[Text] = Field(default_factory=list, max_length=100)
    working_dir: str = Field(default="", max_length=1000)
    user: Text = ""
    hostname: str = Field(default="", max_length=300)
    labels: dict[str, Text] = Field(default_factory=dict, max_length=100)
    ports: list[str] = Field(default_factory=list, max_length=100)
    mounts: list[Mount] = Field(default_factory=list, max_length=100)
    tmpfs: list[str] = Field(default_factory=list, max_length=100)
    restart: str = Field(default="", max_length=30)
    restart_retries: int = Field(default=0, ge=0)
    network_mode: str = Field(default="", max_length=300)
    networks: list[str] = Field(default_factory=list, max_length=100)
    privileged: bool = False
    cap_add: list[str] = Field(default_factory=list, max_length=100)
    cap_drop: list[str] = Field(default_factory=list, max_length=100)
    devices: list[str] = Field(default_factory=list, max_length=100)
    dns: list[str] = Field(default_factory=list, max_length=100)
    extra_hosts: list[str] = Field(default_factory=list, max_length=100)
    security_opt: list[str] = Field(default_factory=list, max_length=100)
    group_add: list[str] = Field(default_factory=list, max_length=100)
    memory: int = Field(default=0, ge=0)
    nano_cpus: int = Field(default=0, ge=0)
    read_only: bool = False
    init: bool = False
    tty: bool = False
    stdin_open: bool = False
    log_driver: str = Field(default="", max_length=60)
    sysctls: dict[str, str] = Field(default_factory=dict, max_length=100)
    pid_mode: str = Field(default="", max_length=60)
    shm_size: int = Field(default=0, ge=0)
    stop_signal: str = Field(default="", max_length=30)
    stop_timeout: int = Field(default=0, ge=0)
    healthcheck: Health | None = None
    truncated: bool = False


class Answer(BaseModel):
    """The answer of `docker.containers.inspect`."""

    model_config = _STRICT
    containers: list[Described] = Field(max_length=20)


class _SecretRef(str):
    """A reference to a `.env` variable, told apart from any text a worker could send."""


_MORE_SECRET = re.compile(r"(KEY|DSN|PWD|JWT|SALT|AUTH|SMTP|COOKIE|SIGNING|CERT)", re.IGNORECASE)
_URL_LOGIN = re.compile(r"://[^/\s:@]*:[^/\s@]+@")


def _looks_secret(key: str, value: str) -> bool:
    """Guess whether a variable holds a secret, from its name or a login inside its value."""
    return bool(
        value and (_SECRET.search(key) or _MORE_SECRET.search(key) or _URL_LOGIN.search(value))
    )


def _escape_all(node: Any) -> Any:
    """Double every `$` in the document (a lone one starts a Compose variable), keys included.

    Done once, over everything, so no field can be forgotten. A secret reference (written as a
    marker while the document was built) becomes `${NAME}` here instead.
    """
    if isinstance(node, dict):
        return {_escape_all(k): _escape_all(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_escape_all(v) for v in node]
    if isinstance(node, str):
        if isinstance(node, _SecretRef):
            return "${" + node + "}"
        return node.replace("$", "$$")
    return node


def _service_name(name: str, taken: set[str]) -> str:
    """Return a Compose service name from a container name, unique among `taken`."""
    base = _SERVICE.sub("-", name.lower()).strip("-._") or "app"
    unique, n = base, 2
    while unique in taken:
        unique, n = f"{base}-{n}", n + 1
    taken.add(unique)
    return unique


def _mount(m: Mount, volumes: dict[str, dict]) -> str | None:
    """Return the Compose short form of a mount, declaring a named volume when needed."""
    mode = "" if m.rw else ":ro"
    if m.type == "bind":
        return f"{m.source}:{m.destination}{mode}"
    if m.type == "volume":
        if _ANONYMOUS.fullmatch(m.name):
            return m.destination  # an anonymous volume: Docker makes a new one
        volumes[m.name] = {"external": True}
        return f"{m.name}:{m.destination}{mode}"
    return None  # tmpfs and others come from their own field or cannot be copied


def _service(c: Described, prefix: str, secrets: dict[str, str], top: dict[str, dict]) -> dict:
    """Build one service from a described container; secrets and top-level parts are collected."""
    s: dict[str, Any] = {"image": c.image, "container_name": c.name}
    if c.entrypoint:
        s["entrypoint"] = list(c.entrypoint)
    if c.command:
        s["command"] = list(c.command)
    env: dict[str, Any] = {}
    for entry in c.env:
        key, equals, value = entry.partition("=")
        if not equals:
            env[key] = None  # passed through from the environment of whoever runs Compose
        elif _looks_secret(key, value) and _KEY.fullmatch(key):
            var, n = f"{prefix}{key}", 2
            while var in secrets:  # `a` + `B_X` and `a-b` + `X` must not share a variable
                var, n = f"{prefix}{key}_{n}", n + 1
            secrets[var] = value
            env[key] = _SecretRef(var)
        else:
            env[key] = value
    if env:
        s["environment"] = env
    if c.ports:
        s["ports"] = c.ports
    mounts = [m for m in (_mount(m, top.setdefault("volumes", {})) for m in c.mounts) if m]
    if mounts:
        s["volumes"] = mounts
    if c.tmpfs:
        s["tmpfs"] = c.tmpfs
    if c.labels:
        s["labels"] = dict(c.labels)
    if c.restart not in ("", "no"):
        s["restart"] = c.restart + (
            f":{c.restart_retries}" if c.restart == "on-failure" and c.restart_retries else ""
        )
    if c.network_mode in ("host", "none") or c.network_mode.startswith("container:"):
        s["network_mode"] = c.network_mode
    else:
        names = [n for n in c.networks if n not in _BRIDGES]
        if names:
            s["networks"] = names
            for n in names:
                top.setdefault("networks", {})[n] = {"external": True}
    for field, key in (
        ("working_dir", "working_dir"),
        ("user", "user"),
        ("hostname", "hostname"),
        ("pid_mode", "pid"),
        ("stop_signal", "stop_signal"),
    ):
        if getattr(c, field):
            s[key] = getattr(c, field)
    for field, key in (
        ("cap_add", "cap_add"),
        ("cap_drop", "cap_drop"),
        ("devices", "devices"),
        ("dns", "dns"),
        ("extra_hosts", "extra_hosts"),
        ("security_opt", "security_opt"),
        ("group_add", "group_add"),
    ):
        if getattr(c, field):
            s[key] = getattr(c, field)
    if c.memory:
        s["mem_limit"] = c.memory
    if c.nano_cpus:
        s["cpus"] = round(c.nano_cpus / _NS, 3)
    if c.shm_size:
        s["shm_size"] = c.shm_size
    if c.stop_timeout:
        s["stop_grace_period"] = f"{c.stop_timeout}s"
    if c.log_driver:
        s["logging"] = {"driver": c.log_driver}
    if c.sysctls:
        s["sysctls"] = c.sysctls
    for flag, key in (
        ("privileged", "privileged"),
        ("read_only", "read_only"),
        ("init", "init"),
        ("tty", "tty"),
        ("stdin_open", "stdin_open"),
    ):
        if getattr(c, flag):
            s[key] = True
    if c.healthcheck:
        h = c.healthcheck
        check: dict[str, Any] = {"test": list(h.test)}
        for value, key in (
            (h.interval, "interval"),
            (h.timeout, "timeout"),
            (h.start_period, "start_period"),
        ):
            if value:
                check[key] = f"{value // _NS}s" if value % _NS == 0 else f"{value // 1_000_000}ms"
        if h.retries:
            check["retries"] = h.retries
        s["healthcheck"] = check
    return s


def convert(containers: list[Described]) -> Result:
    """Turn described containers into one Compose file, a `.env` text and warnings."""
    taken: set[str] = set()
    services: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    top: dict[str, dict] = {}
    warnings: list[dict[str, str]] = []
    many = len(containers) > 1
    prefixes: set[str] = set()
    for c in containers:
        name = _service_name(c.name, taken)
        prefix = ""
        if many:
            base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "APP"
            prefix, n = base + "_", 2
            while prefix in prefixes:  # `a-b` and `a_b` must not share their secrets
                prefix, n = f"{base}_{n}_", n + 1
            prefixes.add(prefix)
        services[name] = _service(c, prefix, secrets, top)
        if c.compose_project:
            warnings.append({"code": "convert.already_compose", "detail": c.compose_project})
        if c.truncated:
            warnings.append({"code": "convert.truncated", "detail": name})
        pairs = [e.partition("=") for e in c.env if "=" in e]
        if any(not _looks_secret(k, v) for k, _, v in pairs):
            warnings.append({"code": "convert.env_inline", "detail": name})
        if any(_looks_secret(k, v) and not _KEY.fullmatch(k) for k, _, v in pairs):
            warnings.append({"code": "convert.secret_inline", "detail": name})
        if c.pid_mode == "host" or c.network_mode == "host" or "SYS_ADMIN" in c.cap_add:
            warnings.append({"code": "convert.host_access", "detail": name})
        if c.privileged:
            warnings.append({"code": "convert.privileged", "detail": name})
        if any(m.type == "bind" and m.source.endswith("docker.sock") for m in c.mounts):
            warnings.append({"code": "convert.docker_socket", "detail": name})
    document: dict[str, Any] = {"services": services}
    document.update({k: v for k, v in top.items() if v})
    compose = yaml.safe_dump(
        _escape_all(document), sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    env_text = "".join(env_line(k, v) for k, v in secrets.items()) or None
    return Result(compose=compose, env=env_text, service=next(iter(services)), warnings=warnings)
