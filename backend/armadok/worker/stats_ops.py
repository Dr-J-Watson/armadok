"""Resource figures: the machine's own, and those of its running containers.

Everything is read, nothing is changed. The machine figures come from `/proc` and `/sys`
(inside a container these still describe the host's processor, memory and network) and from
the free space of the folders the worker was given. Seen from a container, the processor and
memory are the host's, but the network figures are those of the worker's own network
namespace (the host's only with host networking). Only numbers and fixed labels leave the
machine: no path, no process name, no interface name.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from armadok.worker.docker_ops import MAX_CONTAINERS, docker_get
from armadok.worker.operation import Operation, OperationError

PROC = Path("/proc")
SYS = Path("/sys")
SAMPLE_SECONDS = 0.4
MAX_STATS_CONTAINERS = 60
STATS_THREADS = 8
STATS_BUDGET_SECONDS = 15  # the hub gives up at 30 s: the call must end well before that
MAX_FIGURE = 2**50  # what the hub accepts; a daemon "unlimited" (near 2**63) is clamped
MAX_READ_BYTES = 1 << 20
_measuring = threading.Lock()
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")

# Free space is reported for these folders only, under these fixed labels.
FOLDERS = {
    "projects": "ARMADOK_PROJECTS_DIR",
    "traefik": "ARMADOK_TRAEFIK_DIR",
    "backups": "ARMADOK_BACKUP_DIR",
    "data": "ARMADOK_DATA_DIR",
}


def _read(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("ascii", errors="replace")
    except OSError:
        return ""


def _cpu_times() -> tuple[int, int]:
    """Return `(busy, total)` jiffies of the whole machine."""
    fields = _read(PROC / "stat").split("\n", 1)[0].split()[1:]
    values = [int(x) for x in fields[:8] if x.isdigit()]
    if len(values) < 4:
        return 0, 0
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values) - idle, sum(values)


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read(PROC / "meminfo").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[name] = int(parts[0]) * 1024
    return out


def _network() -> tuple[int, int]:
    """Return received and sent bytes over every interface except loopback."""
    received = sent = 0
    for line in _read(PROC / "net" / "dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if (
            name.strip() != "lo"
            and len(fields) >= 9
            and fields[0].isdigit()
            and fields[8].isdigit()
        ):
            received += int(fields[0])
            sent += int(fields[8])
    return received, sent


def _temperature() -> float | None:
    """Return the hottest thermal zone in °C, or None if the machine exposes none."""
    best: float | None = None
    try:
        zones = list((SYS / "class" / "thermal").glob("thermal_zone*/temp"))[:32]
    except OSError:
        return None
    for zone in zones:
        text = _read(zone).strip()
        if text.lstrip("-").isdigit():
            value = int(text) / 1000
            if -50 < value < 200 and (best is None or value > best):
                best = value
    return round(best, 1) if best is not None else None


def _disks() -> list[dict[str, Any]]:
    disks = []
    for label, variable in FOLDERS.items():
        folder = os.environ.get(variable)
        if not folder:
            continue
        try:
            info = os.statvfs(folder)
        except OSError:
            continue
        total = info.f_blocks * info.f_frsize
        if total:
            disks.append({"name": label, "total": total, "free": info.f_bavail * info.f_frsize})
    return disks


def machine_stats(args: dict[str, Any]) -> dict[str, Any]:
    """Return processor, memory, load, disks, network counters, uptime and temperature."""
    busy1, total1 = _cpu_times()
    time.sleep(SAMPLE_SECONDS)
    busy2, total2 = _cpu_times()
    cpu = round(100 * (busy2 - busy1) / (total2 - total1), 1) if total2 > total1 else 0.0
    memory = _meminfo()
    load = _read(PROC / "loadavg").split()
    uptime = _read(PROC / "uptime").split()
    received, sent = _network()
    swap_total = memory.get("SwapTotal", 0)
    return {
        "cpu_percent": min(max(cpu, 0.0), 100.0),
        "cpu_count": os.cpu_count() or 1,
        "load": [float(x) for x in load[:3]] if len(load) >= 3 else [0.0, 0.0, 0.0],
        "memory_total": memory.get("MemTotal", 0),
        "memory_available": memory.get("MemAvailable", 0),
        "swap_total": swap_total,
        "swap_free": memory.get("SwapFree", 0) if swap_total else 0,
        "uptime_seconds": int(float(uptime[0])) if uptime else 0,
        "network_received": received,
        "network_sent": sent,
        "temperature": _temperature(),
        "disks": _disks(),
    }


# --- containers --------------------------------------------------------------------------------


def _count(value: Any) -> int:
    return min(value, MAX_FIGURE) if type(value) is int and value >= 0 else 0


def _container_figures(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce the daemon's statistics of one container to a few numbers."""
    cpu, before = raw.get("cpu_stats") or {}, raw.get("precpu_stats") or {}
    delta = _count((cpu.get("cpu_usage") or {}).get("total_usage")) - _count(
        (before.get("cpu_usage") or {}).get("total_usage")
    )
    system = _count(cpu.get("system_cpu_usage")) - _count(before.get("system_cpu_usage"))
    cores = (
        _count(cpu.get("online_cpus"))
        or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
        or 1
    )
    percent = round(100 * delta / system * cores, 1) if delta > 0 and system > 0 else 0.0
    memory = raw.get("memory_stats") or {}
    cache = _count((memory.get("stats") or {}).get("inactive_file")) or _count(
        (memory.get("stats") or {}).get("cache")
    )
    used = max(_count(memory.get("usage")) - cache, 0)
    received = sent = 0
    for interface in (raw.get("networks") or {}).values():
        if isinstance(interface, dict):
            received += _count(interface.get("rx_bytes"))
            sent += _count(interface.get("tx_bytes"))
    return {
        "cpu_percent": min(percent, 100.0 * cores),
        "memory_used": used,
        "memory_limit": _count(memory.get("limit")),
        "network_received": received,
        "network_sent": sent,
        "pids": _count((raw.get("pids_stats") or {}).get("current")),
    }


def _one(ident: str, deadline: float) -> dict[str, Any] | None:
    left = deadline - time.monotonic()
    if left <= 0:
        return None  # the budget is spent: do not even ask the daemon
    try:
        raw = docker_get(f"/containers/{ident}/stats?stream=false", min(left, 8))
    except OperationError:
        return None  # it stopped meanwhile
    return _container_figures(raw) if isinstance(raw, dict) else None


def docker_stats(args: dict[str, Any]) -> dict[str, Any]:
    """Return processor, memory, network and process figures of the running containers.

    The daemon needs about a second per container to measure processor use, so they are asked
    for a few at a time and the whole call has a budget; containers not measured in time are
    left out and `truncated` says so.
    """
    running = docker_get("/containers/json")
    if not isinstance(running, list):
        raise OperationError("worker.docker_error")
    valid = [
        c for c in running if isinstance(c, dict) and CONTAINER_ID.fullmatch(str(c.get("Id", "")))
    ]
    chosen = valid[: min(MAX_STATS_CONTAINERS, MAX_CONTAINERS)]
    # One measurement at a time on this worker: repeated requests must not queue up behind a
    # slow daemon and hold back every other operation.
    if not _measuring.acquire(blocking=False):
        raise OperationError("worker.busy")
    deadline = time.monotonic() + STATS_BUDGET_SECONDS
    stats: list[dict[str, Any]] = []
    truncated = len(valid) > len(chosen)
    pool = ThreadPoolExecutor(max_workers=STATS_THREADS)
    try:
        futures = [(c, pool.submit(_one, c["Id"], deadline)) for c in chosen]
        for container, future in futures:
            try:
                figures = future.result(timeout=max(deadline - time.monotonic(), 0.1))
            except TimeoutError:
                truncated = True
                continue
            if figures is not None:
                stats.append({"id": container["Id"], **figures})
            else:
                truncated = truncated or time.monotonic() >= deadline
    finally:
        # Do not wait for stragglers: queued work is dropped, running requests end by their own
        # timeout within the budget.
        pool.shutdown(wait=False, cancel_futures=True)
        _measuring.release()
    return {"stats": stats, "truncated": truncated}


OPERATIONS: dict[str, Operation] = {"machine.stats": machine_stats, "docker.stats": docker_stats}
