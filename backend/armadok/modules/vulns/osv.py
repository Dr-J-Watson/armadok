"""Asking the public OSV database about Armadok's own Python packages.

The only outgoing call of this module goes to one fixed host (api.osv.dev), and it carries
nothing but package names and versions. The answer is untrusted text: every field is checked
against a strict pattern or cut to a length before it is kept, and redirects are not followed.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import math
import re
from typing import Any

import httpx

HOST = "https://api.osv.dev"
TIMEOUT_SECONDS = 20
MAX_COMPONENTS = 500
BATCH = 100
MAX_PER_COMPONENT = 200
MAX_DISTINCT_IDS = 1000
SCAN_DEADLINE_SECONDS = 300
MAX_ANSWER_BYTES = 4 * 1024 * 1024
DETAIL_CONCURRENCY = 5
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,63}")
CVE = re.compile(r"CVE-\d{4}-\d{4,9}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}")
SEVERITIES = ("low", "moderate", "high", "critical")


class OsvError(Exception):
    """The database could not be asked, or did not answer in the expected shape."""


def installed_components() -> list[tuple[str, str]]:
    """Return `(name, version)` of every installed Python package (names in OSV's form)."""
    found: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = (dist.metadata["Name"] or "").strip().lower().replace("_", "-")
        version = (dist.version or "").strip()
        if name and VERSION.fullmatch(version):
            found.setdefault(name, version)
    return sorted(found.items())[:MAX_COMPONENTS]


async def _request(
    client: httpx.AsyncClient, method: str, path: str, body: dict | None = None
) -> Any:
    """Send one request and return its JSON, reading at most `MAX_ANSWER_BYTES`."""
    try:
        async with client.stream(method, f"{HOST}{path}", json=body) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_ANSWER_BYTES:
                    raise OsvError("vulns.too_large")
                chunks.append(chunk)
        return json.loads(b"".join(chunks))
    except (httpx.HTTPError, ValueError):
        # A refusal to serve (including "slow down") is a reason to try again later, not an answer.
        raise OsvError("vulns.unreachable") from None


def _client() -> httpx.AsyncClient:
    # No proxy from the environment and no redirect: the request goes to the host written above.
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False)


_CVSS3 = re.compile(r"CVSS:3\.[01]/(?:[A-Z]{1,3}:[A-Z]/?)+")
_WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _round_up(value: float) -> float:
    whole = round(value * 100000)
    return whole / 100000.0 if whole % 10000 == 0 else (math.floor(whole / 10000) + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    """Return the CVSS 3.x base score of a vector, or None if it is not one we can read."""
    if not _CVSS3.fullmatch(vector):
        return None
    parts = dict(item.split(":") for item in vector.split("/")[1:])
    try:
        changed = parts["S"] == "C"
        privileges = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[
            parts["PR"]
        ]
        impact_base = 1 - (1 - _WEIGHTS["C"][parts["C"]]) * (1 - _WEIGHTS["I"][parts["I"]]) * (
            1 - _WEIGHTS["A"][parts["A"]]
        )
        impact = (
            7.52 * (impact_base - 0.029) - 3.25 * (impact_base - 0.02) ** 15
            if changed
            else 6.42 * impact_base
        )
        exploitability = (
            8.22
            * _WEIGHTS["AV"][parts["AV"]]
            * _WEIGHTS["AC"][parts["AC"]]
            * privileges
            * _WEIGHTS["UI"][parts["UI"]]
        )
    except KeyError:
        return None
    if impact <= 0:
        return 0.0
    total = impact + exploitability
    return _round_up(min(1.08 * total, 10) if changed else min(total, 10))


def _level(score: float) -> str:
    return (
        "critical" if score >= 9 else "high" if score >= 7 else "moderate" if score >= 4 else "low"
    )


def parse_detail(raw: Any) -> dict[str, Any] | None:
    """Reduce one OSV record to the fields we keep, or None if it is not a usable record.

    Whatever shape the record has, this never raises: an odd record is simply not usable.
    """
    try:
        return _parse_detail(raw)
    except (TypeError, AttributeError, ValueError, KeyError):
        return None


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _parse_detail(raw: Any) -> dict[str, Any] | None:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("id"), str)
        or not ID.fullmatch(raw["id"])
    ):
        return None
    database = raw.get("database_specific")
    label = str(database.get("severity", "")).lower() if isinstance(database, dict) else ""
    label = "moderate" if label == "medium" else label
    if label not in SEVERITIES:
        # No label from the database: derive one from the CVSS vector, when there is one.
        label = "unknown"
        for entry in _list(raw.get("severity")):
            score = (
                cvss3_score(entry.get("score", ""))
                if isinstance(entry, dict) and isinstance(entry.get("score"), str)
                else None
            )
            if score is not None:
                label = _level(score)
                break
    fixed = ""
    for affected in _list(raw.get("affected")):
        for span in _list(affected.get("ranges") if isinstance(affected, dict) else None):
            for event in _list(span.get("events") if isinstance(span, dict) else None):
                value = event.get("fixed") if isinstance(event, dict) else None
                if isinstance(value, str) and VERSION.fullmatch(value) and not fixed:
                    fixed = value
    aliases = [a for a in _list(raw.get("aliases")) if isinstance(a, str) and CVE.fullmatch(a)]
    return {
        "id": raw["id"],
        "severity": label,
        "summary": " ".join(str(raw.get("summary", "")).split())[:200],
        "fixed": fixed,
        "aliases": aliases[:5],
    }


async def scan(
    components: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Scan `components`; return `(findings, truncated)`.

    There is one finding per (component, vulnerability), and `truncated` holds the components
    that had more vulnerabilities than are kept (their list is incomplete).

    Any answer that is missing, malformed or unusable aborts the whole scan with `OsvError`: a
    partial picture must never be taken for a clean one.
    """
    try:
        async with asyncio.timeout(SCAN_DEADLINE_SECONDS):
            return await _scan(components)
    except TimeoutError:
        raise OsvError("vulns.unreachable") from None


async def _scan(
    components: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    findings: list[dict[str, Any]] = []
    truncated: set[tuple[str, str]] = set()
    async with _client() as client:
        ids_by_component: dict[tuple[str, str], list[str]] = {}
        for start in range(0, len(components), BATCH):
            chunk = components[start : start + BATCH]
            answer = await _request(
                client,
                "POST",
                "/v1/querybatch",
                {
                    "queries": [
                        {"package": {"name": n, "ecosystem": "PyPI"}, "version": v}
                        for n, v in chunk
                    ]
                },
            )
            results = answer.get("results") if isinstance(answer, dict) else None
            if not isinstance(results, list) or len(results) != len(chunk):
                raise OsvError("vulns.bad_answer")
            for component, result in zip(chunk, results, strict=True):
                if not isinstance(result, dict):
                    raise OsvError("vulns.bad_answer")
                listed = _list(result.get("vulns"))
                ids = [
                    v["id"]
                    for v in listed[:MAX_PER_COMPONENT]
                    if isinstance(v, dict)
                    and isinstance(v.get("id"), str)
                    and ID.fullmatch(v["id"])
                ]
                if len(listed) > MAX_PER_COMPONENT:
                    truncated.add(component)
                if ids:
                    ids_by_component[component] = ids
        distinct = {i for ids in ids_by_component.values() for i in ids}
        if len(distinct) > MAX_DISTINCT_IDS:
            raise OsvError("vulns.too_large")
        gate = asyncio.Semaphore(DETAIL_CONCURRENCY)
        cache: dict[str, dict[str, Any]] = {}

        async def detail(vuln_id: str) -> None:
            async with gate:
                info = parse_detail(await _request(client, "GET", f"/v1/vulns/{vuln_id}"))
            if info is None or info["id"] != vuln_id:
                raise OsvError("vulns.bad_answer")  # a listed vulnerability we cannot read
            cache[vuln_id] = info

        await asyncio.gather(*(detail(i) for i in distinct))
    for (name, version), ids in ids_by_component.items():
        for vuln_id in ids:
            findings.append({"component": name, "version": version, **cache[vuln_id]})
    return findings, truncated
