"""`GET /api/v1/metrics`: the hub's state in the Prometheus text format.

Needs the `metrics.read` right, given to an API key that Prometheus presents as a bearer token.
What it shows follows what that key's owner may see: machines they cannot see are absent, and
so are their workers and deployments. Values are counts and states, never names of secrets,
file contents or addresses of visitors.
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from armadok.core.deps import get_db, get_principal, get_registry
from armadok.core.registry import Registry
from armadok.core.rights import Principal
from armadok.modules.nodes import repository as nodes_repo

router = APIRouter(tags=["metrics"])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _label(value: str) -> str:
    """Escape a label value as the exposition format requires."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Page:
    """Builds the text, one metric family at a time."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def family(self, name: str, kind: str, help_text: str, samples: list[tuple[dict, float]]):
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            inner = ",".join(f'{k}="{_label(str(v))}"' for k, v in sorted(labels.items()))
            self.lines.append(f"{name}{{{inner}}} {value:g}" if inner else f"{name} {value:g}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


@router.get("/metrics", summary="Prometheus metrics", response_class=PlainTextResponse)
def metrics(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    registry: Registry = Depends(get_registry),
    who: Principal = Depends(get_principal),
) -> PlainTextResponse:
    """Return the metrics. Needs `metrics.read`; shows only the machines you can see."""
    catalogue = registry.rights()
    who.require("metrics.read", catalogue)
    nodes = [
        n
        for n in nodes_repo.list_nodes(conn)
        if who.has("nodes.read", catalogue, f"node:{n['id']}")
    ]
    visible = {n["id"] for n in nodes}
    page = _Page()
    page.family(
        "armadok_info",
        "gauge",
        "Armadok version.",
        [({"version": os.environ.get("ARMADOK_VERSION", "0.0.0")}, 1)],
    )
    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    page.family(
        "armadok_nodes",
        "gauge",
        "Machines you can see, by type.",
        [({"type": t}, c) for t, c in sorted(by_type.items())],
    )
    hub = getattr(request.app.state, "worker_hub", None)
    page.family(
        "armadok_worker_connected",
        "gauge",
        "1 when the machine's worker is connected.",
        [
            ({"node": n["name"], "node_id": n["id"]}, 1 if hub and hub.get(n["id"]) else 0)
            for n in nodes
        ],
    )
    tasks = {}
    if visible and any(m.name == "projects" for m in registry.modules):
        marks = ",".join("?" * len(visible))
        for row in conn.execute(
            f"SELECT state, COUNT(*) AS n FROM deploy_tasks WHERE node_id IN ({marks}) GROUP BY 1",
            sorted(visible),
        ):
            tasks[row["state"]] = row["n"]
    page.family(
        "armadok_deploy_tasks",
        "gauge",
        "Deployment tasks kept in the history, by state.",
        [({"state": s}, c) for s, c in sorted(tasks.items())],
    )
    clients = getattr(request.app.state, "realtime", None)
    page.family(
        "armadok_live_connections",
        "gauge",
        "Screens connected for live updates.",
        [({}, len(clients.clients) if clients else 0)],
    )
    return PlainTextResponse(page.text(), media_type=CONTENT_TYPE)
