"""Turning Traefik's counters into per-interval figures, and figures into statistics."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from armadok.core.errors import ApiError
from armadok.modules.traffic import repository
from armadok.modules.workers.routes import call_operation

log = logging.getLogger("armadok.traffic")
TICK_SECONDS = 60
STARTUP_DELAY_SECONDS = 90
# A node without metrics (or whose worker is away) is asked again only now and then.
RETRY_AFTER_ERROR = timedelta(minutes=10)
MAX_POINTS = 60
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
MAX_SERVICE_NAMES = 500  # distinct names kept per node over the retention period
MAX_NAME = 200
MAX_HIST_KEYS = 30
CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx", "other"})
_busy: set[str] = set()


# A figure from the worker: finite, not negative, and small enough that sums cannot overflow.
Number = Annotated[float, Field(ge=0, le=1e15, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, le=100000)]


def valid_bound(key: str) -> bool:
    """Tell whether a histogram bound is `+Inf` or a finite number written plainly."""
    if key == "+Inf":
        return True
    if len(key) > 32:
        return False
    try:
        return math.isfinite(float(key))
    except ValueError:
        return False


class ServiceCounters(BaseModel):
    """Cumulative counters of one service, as the worker reports them (untrusted)."""

    model_config = ConfigDict(extra="ignore")
    requests: dict[str, Number] = Field(default_factory=dict, max_length=8)
    buckets: dict[str, Number] = Field(default_factory=dict, max_length=MAX_HIST_KEYS)
    sum: Number = 0.0
    count: Number = 0.0
    bytes_in: Number = 0.0
    bytes_out: Number = 0.0

    @field_validator("requests")
    @classmethod
    def known_classes(cls, value: dict[str, float]) -> dict[str, float]:
        """Keep the status classes we know; anything else the worker invented is dropped."""
        return {k: v for k, v in value.items() if k in CLASSES}

    @field_validator("buckets")
    @classmethod
    def sane_bounds(cls, value: dict[str, float]) -> dict[str, float]:
        """Keep only bounds that are numbers (or `+Inf`): the rest could break the figures."""
        return {k: v for k, v in value.items() if valid_bound(k)}


def _plain_name(name: str) -> bool:
    return 0 < len(name) <= MAX_NAME and name.isprintable()


class Scrape(BaseModel):
    """The worker's answer to `traffic.scrape`."""

    model_config = ConfigDict(extra="ignore")
    configured: bool = True
    services: dict[str, ServiceCounters] = Field(default_factory=dict, max_length=200)
    servers: dict[str, dict[str, Count]] = Field(default_factory=dict, max_length=400)
    reload_ok: bool | None = None
    reload_failures: Number | None = None

    @field_validator("services")
    @classmethod
    def plain_service_names(cls, value: dict[str, ServiceCounters]) -> dict[str, ServiceCounters]:
        """Service names are text to show: refuse long or non-printable ones."""
        return {k: v for k, v in value.items() if _plain_name(k)}

    @field_validator("servers")
    @classmethod
    def plain_server_names(cls, value: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
        """Apply the same rules to the services listed with their servers' state."""
        return {
            k: {s: n for s, n in v.items() if s in ("up", "down")}
            for k, v in value.items()
            if _plain_name(k)
        }


def _positive(value: float) -> float:
    return value if math.isfinite(value) and value > 0 else 0.0


def _delta(now: float, before: float | None) -> float:
    """Return what a counter grew by; a counter that went down was reset (Traefik restarted)."""
    if before is None:
        return 0.0  # the first reading is a baseline, not traffic
    return _positive(now - before if now >= before else now)


def counters_to_state(c: ServiceCounters) -> dict[str, Any]:
    """Return the form in which counters are remembered between two readings."""
    return {
        "requests": c.requests,
        "buckets": c.buckets,
        "sum": c.sum,
        "count": c.count,
        "bytes_in": c.bytes_in,
        "bytes_out": c.bytes_out,
    }


def delta_of(c: ServiceCounters, before: dict[str, Any] | None) -> dict[str, Any]:
    """Return the traffic between two readings of one service.

    With no earlier reading of the service the first one is only a baseline (all zeros). With
    one, a counter that did not exist then (a status class seen for the first time) started
    from zero.
    """
    known = before is not None
    prev = before or {}

    def was(counters: dict[str, Any], key: str) -> float | None:
        return counters.get(key, 0.0) if known else None

    out: dict[str, Any] = {
        "bytes_in": _delta(c.bytes_in, was(prev, "bytes_in")),
        "bytes_out": _delta(c.bytes_out, was(prev, "bytes_out")),
        "dur_sum": _delta(c.sum, was(prev, "sum")),
        "dur_count": _delta(c.count, was(prev, "count")),
        "hist": {
            le: _delta(value, was(prev.get("buckets", {}), le)) for le, value in c.buckets.items()
        },
    }
    for klass, column in repository.CLASS_COLUMN.items():
        out[column] = _delta(c.requests.get(klass, 0.0), was(prev.get("requests", {}), klass))
    return out


async def read_node(app: Any, node_id: str) -> str | None:
    """Read one node's metrics once and store the new traffic; return an error code or None."""
    if node_id in _busy:
        return "traffic.busy"
    _busy.add(node_id)
    try:
        try:
            raw = await call_operation(app, app.state.registry, node_id, "traffic.scrape", {})
            scrape = Scrape.model_validate(raw)
        except ApiError as error:
            code = error.code
        except ValidationError:
            code = "worker.invalid_answer"
        else:
            try:
                await asyncio.to_thread(_store, app, node_id, scrape)
            except Exception:  # noqa: BLE001  (a node deleted during the read, say)
                log.exception("could not store the traffic of a node")
                return "traffic.not_stored"
            return None
        try:
            await asyncio.to_thread(_note_error, app, node_id, code)
        except Exception:  # noqa: BLE001
            log.exception("could not note a failed reading")
        return code
    finally:
        _busy.discard(node_id)


def _note_error(app: Any, node_id: str, code: str) -> None:
    conn = app.state.settings.open_db()
    try:
        repository.note_try(conn, node_id, error=code)
        conn.commit()
    finally:
        conn.close()


def _store(app: Any, node_id: str, scrape: Scrape) -> None:
    conn = app.state.settings.open_db()
    try:
        before = repository.get_state(conn, node_id)
        ts = int(time.time() // repository.STEP * repository.STEP)
        kept = repository.service_names(conn, node_id)
        for name, counters in scrape.services.items():
            if name not in kept and len(kept) >= MAX_SERVICE_NAMES:
                continue  # a node cannot make us keep ever more names
            delta = delta_of(counters, before.get(name))
            if any(delta[k] for k in ("r2", "r3", "r4", "r5", "ro", "dur_count", "bytes_in")):
                repository.add_bucket(conn, node_id, name, ts, delta, MAX_HIST_KEYS)
                kept.add(name)
        repository.set_state(
            conn, node_id, {n: counters_to_state(c) for n, c in scrape.services.items()}
        )
        repository.note_try(
            conn,
            node_id,
            error=None,
            reload_ok=scrape.reload_ok,
            reload_failures=scrape.reload_failures,
            servers=scrape.servers,
        )
        conn.commit()
    finally:
        conn.close()


def quantile(hist: dict[str, float], q: float) -> float | None:
    """Estimate a quantile (seconds) from cumulative bucket counts, interpolating in a bucket."""
    finite = sorted(
        (float(le), c)
        for le, c in hist.items()
        if le != "+Inf" and valid_bound(le) and math.isfinite(c)
    )
    total = max(hist.get("+Inf", 0.0), max((c for _, c in finite), default=0.0))
    if total <= 0 or not finite:
        return None
    target = q * total
    previous_le, previous_count = 0.0, 0.0
    for le, count in finite:
        if count >= target:
            span = count - previous_count
            if span <= 0:
                return le
            return previous_le + (le - previous_le) * (target - previous_count) / span
        previous_le, previous_count = le, count
    return finite[-1][0]  # beyond the last bound: the best we can say


def _history(text: str) -> dict[str, float]:
    """Read a stored histogram, ignoring anything that is not a bound with a finite count."""
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: float(v)
        for k, v in raw.items()
        if isinstance(k, str) and valid_bound(k) and isinstance(v, int | float) and math.isfinite(v)
    }


def _whole(value: float) -> int:
    """Round a figure for display; a non-finite one (never expected) shows as zero."""
    return round(value) if math.isfinite(value) else 0


def statistics(conn: Any, node_id: str, range_name: str) -> dict[str, Any]:
    """Return per-service figures and a short series for a range, with the previous period."""
    window = RANGES[range_name]
    now = int(time.time())
    since, previous_since = now - window, now - 2 * window
    rows = repository.buckets(conn, node_id, previous_since, now + repository.STEP)
    step = max(repository.STEP, math.ceil(window / MAX_POINTS / repository.STEP) * repository.STEP)
    services: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = services.setdefault(
            row["service"],
            {
                "requests": 0.0,
                "errors": 0.0,
                "client_errors": 0.0,
                "bytes_in": 0.0,
                "bytes_out": 0.0,
                "dur_sum": 0.0,
                "dur_count": 0.0,
                "hist": {},
                "previous_requests": 0.0,
                "points": {},
            },
        )
        total = row["r2"] + row["r3"] + row["r4"] + row["r5"] + row["ro"]
        if row["ts"] < since:
            item["previous_requests"] += total
            continue
        item["requests"] += total
        item["errors"] += row["r5"]
        item["client_errors"] += row["r4"]
        item["bytes_in"] += row["bytes_in"]
        item["bytes_out"] += row["bytes_out"]
        item["dur_sum"] += row["dur_sum"]
        item["dur_count"] += row["dur_count"]
        for le, value in _history(row["hist"]).items():
            item["hist"][le] = item["hist"].get(le, 0.0) + value
        slot = (row["ts"] - since) // step
        point = item["points"].setdefault(slot, [0.0, 0.0])
        point[0] += total
        point[1] += row["r5"]
    out = []
    for name, item in sorted(services.items()):
        if item["requests"] == 0 and item["previous_requests"] == 0:
            continue
        slots = range(math.ceil(window / step))
        requests = item["requests"]
        out.append(
            {
                "service": name,
                "requests": _whole(requests),
                "per_second": round(requests / window, 4) if math.isfinite(requests) else 0,
                "errors_5xx": _whole(item["errors"]),
                "errors_4xx": _whole(item["client_errors"]),
                "error_rate": round(item["errors"] / requests, 4) if requests else 0,
                "previous_requests": _whole(item["previous_requests"]),
                "bytes_in": _whole(item["bytes_in"]),
                "bytes_out": _whole(item["bytes_out"]),
                "mean_seconds": round(item["dur_sum"] / item["dur_count"], 4)
                if item["dur_count"]
                else None,
                "p50": quantile(item["hist"], 0.5),
                "p95": quantile(item["hist"], 0.95),
                "p99": quantile(item["hist"], 0.99),
                "series": [
                    [
                        _whole(item["points"].get(s, [0, 0])[0]),
                        _whole(item["points"].get(s, [0, 0])[1]),
                    ]
                    for s in slots
                ],
            }
        )
    out.sort(key=lambda s: -s["requests"])
    return {
        "range": range_name,
        "since": datetime.fromtimestamp(since, UTC).isoformat(),
        "services": out,
    }


async def watch_loop(app: Any) -> None:
    """Every minute, read the metrics of the nodes that have them; trim old figures daily."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    last_purge = 0.0
    while True:
        try:
            conn = app.state.settings.open_db()
            try:
                nodes = repository.nodes_to_read(conn)
            finally:
                conn.close()
            now = datetime.now(UTC)
            for node in nodes:
                if node["last_error"] and node["last_try"]:
                    if now - datetime.fromisoformat(node["last_try"]) < RETRY_AFTER_ERROR:
                        continue
                try:
                    await read_node(app, node["id"])
                except Exception:  # noqa: BLE001  (one node must not stop the others)
                    log.exception("could not read the traffic of a node")
            if time.monotonic() - last_purge > 86400:
                last_purge = time.monotonic()
                conn = app.state.settings.open_db()
                try:
                    repository.purge_old(conn)
                    conn.commit()
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001  (one bad round must not end the loop)
            log.exception("traffic collection failed")
        await asyncio.sleep(TICK_SECONDS)
