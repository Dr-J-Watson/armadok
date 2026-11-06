"""Converting a `docker run` command into a Compose service.

Pure text in, text out: the command is read, never run, and nothing is looked up on a node.
It is split like a shell would split it (no variable or command substitution: `$(...)` stays
the text it is, with a warning), then each flag is mapped to its Compose field. What cannot be
converted is listed, not guessed. Values that look like secrets are moved to a `.env` text so
the Compose file refers to them as `${NAME}` and can be shared safely.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

import yaml

MAX_COMMAND = 8000

# Flags that take a value, by every spelling, mapped to a canonical name.
_VALUE = {
    "-p": "publish",
    "--publish": "publish",
    "-v": "volume",
    "--volume": "volume",
    "-e": "env",
    "--env": "env",
    "--env-file": "env_file",
    "--name": "name",
    "--restart": "restart",
    "--network": "network",
    "--net": "network",
    "-w": "workdir",
    "--workdir": "workdir",
    "-u": "user",
    "--user": "user",
    "--entrypoint": "entrypoint",
    "-m": "memory",
    "--memory": "memory",
    "--cpus": "cpus",
    "--cap-add": "cap_add",
    "--cap-drop": "cap_drop",
    "--device": "device",
    "-l": "label",
    "--label": "label",
    "-h": "hostname",
    "--hostname": "hostname",
    "--dns": "dns",
    "--add-host": "add_host",
    "--pid": "pid",
    "--shm-size": "shm_size",
    "--stop-signal": "stop_signal",
    "--stop-timeout": "stop_timeout",
    "--health-cmd": "health_cmd",
    "--health-interval": "health_interval",
    "--health-timeout": "health_timeout",
    "--health-retries": "health_retries",
    "--health-start-period": "health_start",
    "--log-driver": "log_driver",
    "--platform": "platform",
    "--security-opt": "security_opt",
    "--sysctl": "sysctl",
    "--tmpfs": "tmpfs",
    "--group-add": "group_add",
    "--expose": "expose",
}
_FLAG = {
    "-d": "detach",
    "--detach": "detach",
    "-i": "stdin",
    "--interactive": "stdin",
    "-t": "tty",
    "--tty": "tty",
    "--rm": "rm",
    "--privileged": "privileged",
    "--read-only": "read_only",
    "--init": "init",
}
# Accepted and meaningless in Compose: nothing to say about them.
_IGNORED_VALUE = {"--pull", "--log-opt", "--detach-keys"}
_SECRET = re.compile(r"(PASS(WORD)?|SECRET|TOKEN|API_?KEY|PRIVATE|CREDENTIAL)", re.IGNORECASE)
_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")
_BRIDGES = {"host", "none", "bridge"}


_PLAIN = re.compile(r"[A-Za-z0-9_.,:/@%+=-]*")


def env_line(key: str, value: str) -> str:
    """Return one `.env` line; a value with anything unusual is double-quoted and escaped.

    A line break in a value would otherwise start a new variable, and a `$` would be read as
    a reference to another one.
    """
    if _PLAIN.fullmatch(value):
        return f"{key}={value}\n"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'{key}="{escaped}"\n'


class Result(dict):
    """The conversion: `compose` (text), `env` (text or None) and `warnings` (code, detail)."""


def _split(command: str) -> list[str]:
    if len(command) > MAX_COMMAND:
        raise ValueError("convert.too_long")
    # A shell would run `$(...)` and backticks before docker ever saw them: reading them as plain
    # words would give a wrong file, so such a command is refused (the person writes the value).
    if "$(" in command or "`" in command:
        raise ValueError("convert.substitution")
    # A trailing backslash continues the line, as in a shell.
    text = re.sub(r"\\\s*\n", " ", command.strip())
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        raise ValueError("convert.unparsable") from None
    while tokens and tokens[0] in ("sudo",):
        tokens.pop(0)
    if tokens[:2] == ["docker", "run"] or tokens[:3] == ["docker", "container", "run"]:
        tokens = tokens[2:] if tokens[1] == "run" else tokens[3:]
    elif tokens[:1] == ["docker"]:
        raise ValueError("convert.not_run")
    return tokens


def _parse(
    tokens: list[str],
) -> tuple[dict[str, Any], str | None, list[str], list[tuple[str, str]]]:
    """Return `(options, image, command words, warnings)`."""
    options: dict[str, Any] = {}
    warnings: list[tuple[str, str]] = []
    i = 0

    def add(key: str, value: str) -> None:
        options.setdefault(key, []).append(value)

    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            i += 1
            break
        if not token.startswith("-") or token == "-":
            break
        name, equals, attached = token.partition("=")
        if name in _VALUE or (
            len(token) > 2 and token[:2] in _VALUE and not token.startswith("--")
        ):
            if name not in _VALUE:  # `-p8080:80`: the value is stuck to the letter
                name, attached, equals = token[:2], token[2:], "="
            if equals:
                value = attached
            else:
                i += 1
                if i >= len(tokens):
                    warnings.append(("convert.missing_value", name))
                    break
                value = tokens[i]
            add(_VALUE[name], value)
        elif name in _FLAG:
            options[_FLAG[name]] = True
        elif not token.startswith("--") and all(f"-{c}" in _FLAG for c in token[1:]):
            for c in token[1:]:  # `-dit`
                options[_FLAG[f"-{c}"]] = True
        elif name in _IGNORED_VALUE:
            if not equals:
                i += 1
        else:
            warnings.append(("convert.unsupported_flag", name))
        i += 1
    if i >= len(tokens):
        return options, None, [], warnings
    return options, tokens[i], tokens[i + 1 :], warnings


def _ports(values: list[str]) -> list[str]:
    return [v for v in values]


def convert(command: str) -> Result:
    """Convert one `docker run` command. Raises `ValueError(code)` when it cannot be read."""
    tokens = _split(command)
    options, image, words, warn = _parse(tokens)
    if image is None:
        raise ValueError("convert.no_image")
    warnings = list(warn)
    service: dict[str, Any] = {"image": image}
    secrets: dict[str, str] = {}
    top_volumes: dict[str, dict] = {}
    top_networks: dict[str, dict] = {}

    name = options.get("name", [None])[-1]
    base = name or _NAME.sub("-", image.rsplit("/", 1)[-1].split(":")[0].split("@")[0]) or "app"
    if name:
        service["container_name"] = name
    if options.get("publish"):
        service["ports"] = _ports(options["publish"])
    if options.get("expose"):
        service["expose"] = options["expose"]
    if options.get("volume"):
        mounts = []
        for volume in options["volume"]:
            source = volume.split(":", 1)[0]
            if source and not source.startswith(("/", ".", "~", "$")):
                top_volumes[source] = {}  # a named volume must be declared too
            mounts.append(volume)
        service["volumes"] = mounts
    if options.get("env"):
        env: dict[str, Any] = {}
        for entry in options["env"]:
            key, equals, value = entry.partition("=")
            if equals and _SECRET.search(key):
                secrets[key] = value
                env[key] = "${" + key + "}"
            else:
                env[key] = value if equals else None
        service["environment"] = env
    if options.get("env_file"):
        service["env_file"] = options["env_file"]
    if options.get("restart"):
        service["restart"] = options["restart"][-1]
    if options.get("network"):
        network = options["network"][-1]
        if network in _BRIDGES or network.startswith("container:"):
            service["network_mode"] = network
        else:
            service["networks"] = [network]
            top_networks[network] = {"external": True}
    for field, key in (
        ("workdir", "working_dir"),
        ("user", "user"),
        ("hostname", "hostname"),
        ("pid", "pid"),
        ("shm_size", "shm_size"),
        ("stop_signal", "stop_signal"),
        ("platform", "platform"),
    ):
        if options.get(field):
            service[key] = options[field][-1]
    if options.get("memory"):
        service["mem_limit"] = options["memory"][-1]
    if options.get("cpus"):
        service["cpus"] = options["cpus"][-1]
    if options.get("stop_timeout"):
        service["stop_grace_period"] = f"{options['stop_timeout'][-1]}s"
    for field, key in (
        ("cap_add", "cap_add"),
        ("cap_drop", "cap_drop"),
        ("device", "devices"),
        ("dns", "dns"),
        ("add_host", "extra_hosts"),
        ("security_opt", "security_opt"),
        ("tmpfs", "tmpfs"),
        ("group_add", "group_add"),
    ):
        if options.get(field):
            service[key] = options[field]
    if options.get("label"):
        service["labels"] = dict(
            (k, v) for k, _, v in (entry.partition("=") for entry in options["label"])
        )
    if options.get("sysctl"):
        service["sysctls"] = dict(
            (k, v) for k, _, v in (entry.partition("=") for entry in options["sysctl"])
        )
    if options.get("log_driver"):
        service["logging"] = {"driver": options["log_driver"][-1]}
    if options.get("entrypoint"):
        entry = options["entrypoint"][-1]
        service["entrypoint"] = shlex.split(entry) if " " in entry else [entry]
    if words:
        service["command"] = words
    if options.get("health_cmd"):
        health: dict[str, Any] = {"test": ["CMD-SHELL", options["health_cmd"][-1]]}
        for field, key in (
            ("health_interval", "interval"),
            ("health_timeout", "timeout"),
            ("health_start", "start_period"),
        ):
            if options.get(field):
                health[key] = options[field][-1]
        if options.get("health_retries"):
            health["retries"] = (
                int(options["health_retries"][-1])
                if options["health_retries"][-1].isdigit()
                else options["health_retries"][-1]
            )
        service["healthcheck"] = health
    for flag, key in (
        ("privileged", "privileged"),
        ("read_only", "read_only"),
        ("init", "init"),
        ("tty", "tty"),
        ("stdin", "stdin_open"),
    ):
        if options.get(flag):
            service[key] = True
    if options.get("rm"):
        warnings.append(("convert.rm", ""))
    if not options.get("detach"):
        warnings.append(("convert.foreground", ""))
    document: dict[str, Any] = {"services": {base: service}}
    if top_volumes:
        document["volumes"] = top_volumes
    if top_networks:
        document["networks"] = top_networks
    compose = yaml.safe_dump(
        document, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    env_text = "".join(env_line(k, v) for k, v in secrets.items()) or None
    return Result(
        compose=compose,
        env=env_text,
        service=base,
        warnings=[{"code": code, "detail": detail} for code, detail in warnings],
    )
