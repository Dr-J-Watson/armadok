"""Checks that the Traefik files agree with each other: broken references, loops, leftovers.

Pure functions over what `parse.summarize` returns, so they can be tested alone. Only names that
live in the files (`@file`) can be checked: a reference to `@docker`, `@internal` or another
provider is created elsewhere and is neither blamed nor verified.
"""

from __future__ import annotations

from typing import Any

FILE = "@file"
MAX_ISSUES = 300


def _mine(ident: str) -> bool:
    return ident.endswith(FILE)


def analyse(summaries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the problems found, worst first: `{severity, code, subject, path, detail}`."""
    routers = [r for s in summaries for r in s["routers"]]
    services = [x for s in summaries for x in s["services"]]
    middlewares = [m for s in summaries for m in s["middlewares"]]
    issues: list[dict[str, str]] = []

    def add(severity: str, code: str, subject: str, path: str, detail: str = "") -> None:
        issues.append(
            {"severity": severity, "code": code, "subject": subject, "path": path, "detail": detail}
        )

    # Two files defining the same name: Traefik keeps one and drops the other, silently.
    for kind, items in (("router", routers), ("service", services), ("middleware", middlewares)):
        seen: dict[str, str] = {}
        for item in items:
            if item["id"] in seen:
                add("error", f"duplicate.{kind}", item["id"], item["path"], seen[item["id"]])
            seen.setdefault(item["id"], item["path"])

    service_ids = {x["id"] for x in services}
    middleware_ids = {m["id"] for m in middlewares}
    used_services: set[str] = set()
    used_middlewares: set[str] = set()
    for r in routers:
        if not r["rule"]:
            add("error", "router.no_rule", r["id"], r["path"])
        if not r["service"]:
            add("error", "router.no_service", r["id"], r["path"])
        elif _mine(r["service"]):
            used_services.add(r["service"])
            if r["service"] not in service_ids:
                add("error", "router.service_missing", r["id"], r["path"], r["service"])
        for ref in r["middlewares"]:
            if _mine(ref):
                used_middlewares.add(ref)
                if ref not in middleware_ids:
                    add("error", "router.middleware_missing", r["id"], r["path"], ref)

    by_id = {m["id"]: m for m in middlewares}
    for m in middlewares:
        for ref in m.get("refs", []):
            if _mine(ref):
                used_middlewares.add(ref)
                if ref not in middleware_ids:
                    add("error", "chain.middleware_missing", m["id"], m["path"], ref)
    for loop in _loops(by_id):
        add("error", "chain.loop", loop[0], by_id[loop[0]]["path"], " → ".join(loop + [loop[0]]))

    # The same site declared twice: the second router never gets the traffic it expects.
    hosts: dict[tuple[str, str], str] = {}
    for r in routers:
        if r["rule"] and "Host(" in r["rule"] and "PathPrefix" not in r["rule"]:
            key = (r["rule"].strip(), ",".join(sorted(r["entrypoints"])))
            if key in hosts and hosts[key] != r["id"]:
                add("warning", "router.duplicate_rule", r["id"], r["path"], hosts[key])
            hosts.setdefault(key, r["id"])

    for x in services:
        if x["id"] not in used_services:
            add("info", "service.unused", x["id"], x["path"])
    for m in middlewares:
        if m["id"] not in used_middlewares:
            add("info", "middleware.unused", m["id"], m["path"])
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["severity"]], i["code"], i["subject"]))
    return issues[:MAX_ISSUES]


def _loops(by_id: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Find chains that lead back to themselves; one entry per loop, starting at its smallest id."""
    found: dict[frozenset[str], list[str]] = {}
    done: set[str] = set()

    def walk(node: str, trail: list[str]) -> None:
        if node in trail:
            cycle = trail[trail.index(node) :]
            start = cycle.index(min(cycle))
            found.setdefault(frozenset(cycle), cycle[start:] + cycle[:start])
            return
        if node in done or node not in by_id:
            return
        for ref in by_id[node].get("refs", []):
            walk(ref, [*trail, node])
        done.add(node)

    for ident in sorted(by_id):
        walk(ident, [])
    return list(found.values())
