"""The `armadok` command: `python -m armadok.cli` (or the `armadok` script).

Exit codes: 0 done, 1 the server refused, 2 a usage or setup problem, 3 an action that ran and
failed (`deploy` reports the outcome of the task). `--json` prints machine-readable output.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import unicodedata
from typing import Any
from urllib.parse import quote

from armadok.cli import client

_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
POLL_SECONDS = 2.0


def clean(value: Any) -> str:
    """Make server-controlled text safe to print: no escape sequence reaches the terminal.

    Container logs, task output and names come from other programs. An escape code in them
    could rewrite the screen, set the title or reach the clipboard. Line breaks and tabs stay;
    every other control character (C0, C1, DEL) and the bidirectional overrides become a
    visible placeholder.
    """
    text = str(value)
    return "".join(
        ch
        if ch in "\n\t" or (unicodedata.category(ch) not in ("Cc", "Cf") and ch != "\x7f")
        else "\ufffd"
        for ch in text
    )


def _dump(data: Any) -> str:
    """JSON for people and scripts: plain ASCII, so no control character can pass."""
    return json.dumps(data, indent=2, ensure_ascii=True)


def _api(method: str, path: str, body: Any = None) -> Any:
    url, key = client.load_settings()
    return client.call(method, path, body, url=url, key=key)


def _show(rows: list[list[str]], header: list[str], as_json: bool, data: Any) -> None:
    """Print a table, or the raw data as JSON."""
    if as_json:
        print(_dump(data))
        return
    rows = [[clean(cell) for cell in row] for row in rows]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def _check(value: str, pattern: re.Pattern, label: str) -> str:
    if not pattern.fullmatch(value):
        raise client.CliError("cli.bad_argument", f"{label} is not valid.")
    return value


def cmd_login(args: argparse.Namespace) -> int:
    """Remember the address and key (the key is asked without echo, never taken from argv)."""
    url = client.check_url(args.url)
    key = (os.environ.get(client.ENV_KEY) or getpass.getpass("API key: ")).strip()
    client.check_key(key)
    me = client.call("GET", "/auth/me", url=url, key=key)  # test it before keeping it
    path = client.save_config(url, key)
    print(
        f"Signed in as {me.get('username', '?')}. Settings kept in {path} (readable by you only)."
    )
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Show which account the key stands for."""
    me = _api("GET", "/auth/me")
    if args.json:
        print(_dump({k: me.get(k) for k in ("username", "kind")}))
    else:
        print(f"{clean(me.get('username'))} ({clean(me.get('kind', 'person'))})")
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    """List the machines you can see."""
    nodes = _api("GET", "/nodes")
    _show(
        [[n["id"], n["name"], n["type"]] for n in nodes], ["ID", "NAME", "TYPE"], args.json, nodes
    )
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    """List the Compose projects of a machine."""
    node = _check(args.node, _ID, "The machine")
    data = _api("GET", f"/nodes/{node}/projects")
    projects = data.get("projects", [])
    _show([[p["name"]] for p in projects], ["PROJECT"], args.json, projects)
    return 0


def cmd_containers(args: argparse.Namespace) -> int:
    """List the containers of a machine."""
    node = _check(args.node, _ID, "The machine")
    data = _api("GET", f"/nodes/{node}/docker/containers")
    rows = [[(c["names"] or [c["id"][:12]])[0], c["state"], c["image"]] for c in data["containers"]]
    _show(rows, ["NAME", "STATE", "IMAGE"], args.json, data["containers"])
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Print the last lines of a container's logs."""
    node = _check(args.node, _ID, "The machine")
    container = _check(
        args.container,
        re.compile(r"[0-9a-f]{12,64}|[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"),
        "The container",
    )
    data = _api(
        "GET", f"/nodes/{node}/docker/containers/{quote(container)}/logs?tail={int(args.tail)}"
    )
    sys.stdout.write(clean(data.get("output", "")))
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    """Run an action on a Compose project and (unless --no-wait) follow it to its end."""
    node = _check(args.node, _ID, "The machine")
    project = _check(args.project, _NAME, "The project")
    body: dict[str, Any] = {"action": args.action}
    if args.action == "up":
        body.update(build=args.build, pull=args.pull)
    if args.service:
        body["service"] = _check(args.service, _NAME, "The service")
    started = _api("POST", f"/nodes/{node}/projects/{project}/actions", body)
    task = started["task_id"]
    if args.no_wait:
        print(task)
        return 0
    # The handle is given at once: if following fails, the action is still running and findable.
    print(f"task {task}", file=sys.stderr)
    deadline = time.monotonic() + min(max(args.timeout, 1), 3600)
    while time.monotonic() < deadline:
        state = _api("GET", f"/nodes/{node}/tasks/{task}")
        if state["state"] != "running":
            if args.json:
                print(_dump(state))
            else:
                sys.stdout.write(clean(state.get("output", "")))
                print(f"\n{args.action} {project}: {clean(state['state'])}")
            return 0 if state["state"] == "success" else 3
        time.sleep(POLL_SECONDS)
    raise client.CliError(
        "cli.timeout", f"Still running after {args.timeout} s; task {task} continues."
    )


def cmd_api(args: argparse.Namespace) -> int:
    """Call any endpoint: the way to use what has no command yet."""
    raw = args.data
    if raw == "@-":  # a body with a secret in it stays out of the process list and the history
        raw = sys.stdin.read(1024 * 1024)
    elif raw and raw.startswith("@"):
        try:
            with open(raw[1:], encoding="utf-8") as handle:
                raw = handle.read(1024 * 1024)
        except OSError:
            raise client.CliError("cli.bad_argument", "That file cannot be read.") from None
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        raise client.CliError("cli.bad_argument", "The data is not valid JSON.") from None
    result = _api(args.method.upper(), args.path, body)
    if result is not None:
        print(_dump(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Define the commands."""
    parser = argparse.ArgumentParser(prog="armadok", description=__doc__)
    parser.add_argument("--debug", action="store_true", help="show the kind of an unexpected error")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Any, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="print JSON")
        p.set_defaults(func=func)
        return p

    p = add("login", cmd_login, "remember the server address and an API key")
    p.add_argument("url", help="e.g. https://armadok.example.com")
    add("whoami", cmd_whoami, "show the account behind the key")
    add("nodes", cmd_nodes, "list machines")
    p = add("projects", cmd_projects, "list Compose projects of a machine")
    p.add_argument("node")
    p = add("containers", cmd_containers, "list containers of a machine")
    p.add_argument("node")
    p = add("logs", cmd_logs, "print a container's recent logs")
    p.add_argument("node")
    p.add_argument("container")
    p.add_argument("--tail", type=int, default=200)
    p = add("deploy", cmd_deploy, "run an action on a Compose project")
    p.add_argument("node")
    p.add_argument("project")
    p.add_argument(
        "--action",
        default="up",
        choices=["up", "down", "stop", "start", "restart", "pull", "build"],
    )
    p.add_argument("--service")
    p.add_argument("--build", action="store_true")
    p.add_argument("--pull", action="store_true")
    p.add_argument("--no-wait", action="store_true", help="print the task id and return")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait (default 900)")
    p = add("api", cmd_api, "call any endpoint")
    p.add_argument(
        "method",
        choices=["get", "post", "put", "patch", "delete", "GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    p.add_argument("path", help="e.g. /nodes")
    p.add_argument("--data", help="JSON body; @file or @- reads it from a file or stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command; return the exit code."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except client.CliError as error:
        print(f"armadok: {clean(error.message)}", file=sys.stderr)
        return 1 if error.status else 2
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001
        # Never a traceback: it can quote a request (the key) or server text. The kind of error
        # is shown only on request.
        detail = f" ({type(error).__name__})" if args.debug else ""
        print(f"armadok: unexpected problem{detail}. Run with --debug for more.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
