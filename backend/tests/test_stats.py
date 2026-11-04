import json
import logging
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.worker import stats_ops

PASSWORD = "correct horse battery"
ID1, ID2 = "a" * 64, "b" * 64


def fake_proc(tmp_path: Path, monkeypatch, cpu=("100 0 100 800 0 0 0 0", "150 0 150 900 0 0 0 0")):
    proc, sys_ = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (proc / "meminfo").write_text(
        "MemTotal: 8000 kB\nMemAvailable: 6000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
    )
    (proc / "loadavg").write_text("0.50 0.40 0.30 1/200 999\n")
    (proc / "uptime").write_text("90061.55 12345.67\n")
    (proc / "net" / "dev").write_text(
        "Inter-|   Receive |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n"
        "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
        "  eth0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n"
    )
    zone = sys_ / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("51500\n")
    states = iter(cpu)

    def stat():
        return f"cpu  {next(states)}\n"

    monkeypatch.setattr(stats_ops, "PROC", proc)
    monkeypatch.setattr(stats_ops, "SYS", sys_)
    monkeypatch.setattr(stats_ops, "SAMPLE_SECONDS", 0)
    original = stats_ops._read

    def read(path):
        return stat() if path == proc / "stat" else original(path)

    monkeypatch.setattr(stats_ops, "_read", read)
    return proc


def test_machine_figures_are_read_from_proc_and_reduced_to_numbers(tmp_path, monkeypatch):
    fake_proc(tmp_path, monkeypatch)
    monkeypatch.setenv("ARMADOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.delenv("ARMADOK_TRAEFIK_DIR", raising=False)
    answer = stats_ops.machine_stats({})
    assert answer["cpu_percent"] == pytest.approx(50.0)  # 100 busy of 200 elapsed jiffies
    assert answer["memory_total"] == 8000 * 1024 and answer["memory_available"] == 6000 * 1024
    assert answer["load"] == [0.5, 0.4, 0.3] and answer["uptime_seconds"] == 90061
    assert (answer["network_received"], answer["network_sent"]) == (1000, 2000)  # loopback left out
    assert answer["temperature"] == 51.5
    assert [d["name"] for d in answer["disks"]] == ["projects"]
    assert str(tmp_path) not in json.dumps(answer)  # no path leaves the machine


def test_a_machine_without_sensors_or_proc_still_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_ops, "PROC", tmp_path / "none")
    monkeypatch.setattr(stats_ops, "SYS", tmp_path / "none")
    monkeypatch.setattr(stats_ops, "SAMPLE_SECONDS", 0)
    answer = stats_ops.machine_stats({})
    assert answer["temperature"] is None and answer["memory_total"] == 0
    assert answer["load"] == [0.0, 0.0, 0.0] and answer["cpu_percent"] == 0.0


def test_container_figures_are_computed_and_clamped():
    raw = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 300},
            "system_cpu_usage": 2000,
            "online_cpus": 2,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 5000, "limit": 10000, "stats": {"inactive_file": 1000}},
        "networks": {
            "eth0": {"rx_bytes": 10, "tx_bytes": 20},
            "eth1": {"rx_bytes": 1, "tx_bytes": 2},
        },
        "pids_stats": {"current": 7},
    }
    figures = stats_ops._container_figures(raw)
    assert figures == {
        "cpu_percent": 40.0,
        "memory_used": 4000,
        "memory_limit": 10000,
        "network_received": 11,
        "network_sent": 22,
        "pids": 7,
    }
    hostile = {
        "cpu_stats": {"cpu_usage": {"total_usage": -5}},
        "memory_stats": {"usage": "lots"},
        "networks": {"x": 3},
    }
    assert stats_ops._container_figures(hostile)["memory_used"] == 0


class Daemon(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/containers/json":
            body = [{"Id": ID1}, {"Id": ID2}, {"Id": "not-an-id"}]
        elif self.path == f"/containers/{ID1}/stats?stream=false":
            body = {"memory_stats": {"usage": 100, "limit": 200}, "pids_stats": {"current": 3}}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    path = str(tmp_path / "d.sock")
    server = Server(path, Daemon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", path)
    yield
    server.shutdown()


def test_stats_cover_the_running_containers_and_skip_one_that_vanished(daemon):
    answer = stats_ops.docker_stats({})
    assert [s["id"] for s in answer["stats"]] == [ID1]  # ID2 answered 404: it stopped meanwhile
    assert answer["stats"][0]["memory_used"] == 100 and answer["stats"][0]["pids"] == 3
    assert answer["truncated"] is False


def test_too_many_containers_are_cut_and_said_so(daemon, monkeypatch):
    monkeypatch.setattr(stats_ops, "MAX_STATS_CONTAINERS", 1)
    assert stats_ops.docker_stats({})["truncated"] is True


@pytest.fixture
def box(tmp_path, caplog, daemon):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                cookie_secure=False,
                local_node=True,
                encryption_key=generate_key(),
            )
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        ).json()["access_token"]
        admin = {"Authorization": f"Bearer {token}"}
        yield client, admin, client.get("/api/v1/nodes", headers=admin).json()[0]["id"]


def person(client, admin, name, grants=()):
    uid = client.post(
        "/api/v1/users", headers=admin, json={"username": name, "password": PASSWORD}
    ).json()["id"]
    for right, scope in grants:
        client.post(
            f"/api/v1/users/{uid}/grants", headers=admin, json={"right": right, "scope": scope}
        )
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_the_machine_endpoint_validates_and_respects_rights(box):
    client, admin, node = box
    got = client.get(f"/api/v1/nodes/{node}/machine", headers=admin)
    assert got.status_code == 200 and got.json()["cpu_count"] >= 1
    scope = f"node:{node}"
    reader = person(client, admin, "reader", [("nodes.read", scope)])
    stranger = person(client, admin, "stranger")
    assert client.get(f"/api/v1/nodes/{node}/machine", headers=reader).status_code == 403
    assert client.get(f"/api/v1/nodes/{node}/machine", headers=stranger).status_code == 404


def test_the_docker_stats_endpoint_validates_and_respects_rights(box):
    client, admin, node = box
    got = client.get(f"/api/v1/nodes/{node}/docker/stats", headers=admin)
    assert got.status_code == 200 and [s["id"] for s in got.json()["stats"]] == [ID1]
    stranger = person(client, admin, "stranger")
    assert client.get(f"/api/v1/nodes/{node}/docker/stats", headers=stranger).status_code == 404


# --- review points ----------------------------------------------------------------------------


def test_a_slow_daemon_cannot_hold_the_measurement_past_its_budget(monkeypatch):
    import time

    monkeypatch.setattr(stats_ops, "STATS_BUDGET_SECONDS", 0.3)
    ids = [f"{i:064x}" for i in range(30)]
    monkeypatch.setattr(
        stats_ops,
        "docker_get",
        lambda path, timeout=10: (
            [{"Id": i} for i in ids] if path == "/containers/json" else time.sleep(0.5) or {}
        ),
    )
    started = time.monotonic()
    answer = stats_ops.docker_stats({})
    assert time.monotonic() - started < 1.5  # not 30 containers × 0.5 s / 8 threads
    assert answer["truncated"] is True


def test_only_one_measurement_runs_at_a_time_on_a_worker(monkeypatch):
    assert stats_ops._measuring.acquire(blocking=False)
    try:
        monkeypatch.setattr(stats_ops, "docker_get", lambda *a, **k: [])
        with pytest.raises(stats_ops.OperationError) as error:
            stats_ops.docker_stats({})
        assert error.value.code == "worker.busy"
    finally:
        stats_ops._measuring.release()


def test_an_unlimited_memory_figure_is_clamped_not_refused():
    figures = stats_ops._container_figures({"memory_stats": {"usage": 5, "limit": 2**63 - 1}})
    assert figures["memory_limit"] == 2**50


def test_the_hub_refuses_incoherent_or_duplicated_figures():
    from pydantic import ValidationError

    from armadok.modules.docker.schemas import ContainerStats
    from armadok.modules.machine.routes import Disk, MachineStats

    one = {
        "id": ID1,
        "cpu_percent": 1.0,
        "memory_used": 1,
        "memory_limit": 2,
        "network_received": 0,
        "network_sent": 0,
        "pids": 1,
    }
    with pytest.raises(ValidationError):
        ContainerStats.model_validate({"stats": [one, one]})
    with pytest.raises(ValidationError):
        ContainerStats.model_validate({"stats": [{**one, "id": "a" * 12}]})
    with pytest.raises(ValidationError):
        Disk.model_validate({"name": "data", "total": 10, "free": 11})
    base = {
        "cpu_percent": 1.0,
        "cpu_count": 1,
        "load": [0, 0, 0],
        "memory_total": 10,
        "memory_available": 5,
        "swap_total": 0,
        "swap_free": 0,
        "uptime_seconds": 1,
        "network_received": 0,
        "network_sent": 0,
        "disks": [],
    }
    MachineStats.model_validate(base)
    for bad in (
        {"memory_available": 11},
        {"load": [float("nan"), 0, 0]},
        {"load": [-1, 0, 0]},
        {"swap_free": 1},
    ):
        with pytest.raises(ValidationError):
            MachineStats.model_validate({**base, **bad})


def test_many_viewers_share_one_measurement_and_failures_are_not_kept():
    import asyncio

    from armadok.core.coalesce import Shared

    async def scenario():
        shared, calls = Shared(60), []

        async def make():
            calls.append(1)
            await asyncio.sleep(0.05)
            return {"n": len(calls)}

        results = await asyncio.gather(*(shared.get("node", make) for _ in range(10)))
        assert len(calls) == 1 and all(r == {"n": 1} for r in results)
        assert await shared.get("node", make) == {"n": 1}  # kept
        assert len(calls) == 1
        # A failure is shared by those waiting, then forgotten.
        failing = Shared(60)
        attempts = []

        async def broken():
            attempts.append(1)
            await asyncio.sleep(0.02)
            raise ValueError("boom")

        outcomes = await asyncio.gather(
            *(failing.get("n", broken) for _ in range(3)), return_exceptions=True
        )
        assert len(attempts) == 1 and all(isinstance(o, ValueError) for o in outcomes)
        with pytest.raises(ValueError):
            await failing.get("n", broken)
        assert len(attempts) == 2
        # One caller giving up does not cancel the run for the others.
        patient = Shared(60)

        async def slow():
            await asyncio.sleep(0.1)
            return "done"

        first = asyncio.ensure_future(patient.get("n", slow))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(patient.get("n", slow))
        first.cancel()
        assert await second == "done"

    asyncio.run(scenario())
