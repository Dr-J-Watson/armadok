"""Traffic statistics: reading Traefik's metrics, turning counters into figures, the endpoints."""

import logging
import re
import time

import pytest
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.main import create_app
from armadok.modules.traffic import repository, routes, service
from armadok.worker import traffic_ops
from armadok.worker.operation import OperationError
from tests.test_members_admin import PASSWORD, login, owner_with_node, person

METRICS = """\
# HELP traefik_service_requests_total How many HTTP requests processed.
# TYPE traefik_service_requests_total counter
traefik_service_requests_total{code="200",method="GET",protocol="http",service="blog@file"} 90
traefik_service_requests_total{code="404",method="GET",protocol="http",service="blog@file"} 6
traefik_service_requests_total{code="502",method="GET",protocol="http",service="blog@file"} 4
traefik_service_request_duration_seconds_bucket{code="200",service="blog@file",le="0.1"} 80
traefik_service_request_duration_seconds_bucket{code="200",service="blog@file",le="1"} 98
traefik_service_request_duration_seconds_bucket{code="200",service="blog@file",le="+Inf"} 100
traefik_service_request_duration_seconds_sum{code="200",service="blog@file"} 12.5
traefik_service_request_duration_seconds_count{code="200",service="blog@file"} 100
traefik_service_requests_bytes_total{service="blog@file",code="200"} 2000
traefik_service_responses_bytes_total{service="blog@file",code="200"} 90000
traefik_service_server_up{service="blog@file",url="http://blog:80"} 1
traefik_service_server_up{service="shop@file",url="http://shop:80"} 0
traefik_config_last_reload_success 1.7938e+09
traefik_config_last_reload_failure 1.7937e+09
traefik_config_reloads_failure_total 2
"""


# --- the worker reads and reduces the metrics ------------------------------


def test_the_metrics_are_reduced_to_counters_per_service():
    got = traffic_ops.parse(METRICS)
    blog = got["services"]["blog@file"]
    assert blog["requests"] == {"2xx": 90.0, "4xx": 6.0, "5xx": 4.0}
    assert blog["buckets"] == {"0.1": 80.0, "1": 98.0, "+Inf": 100.0}
    assert blog["sum"] == 12.5 and blog["count"] == 100.0
    assert (blog["bytes_in"], blog["bytes_out"]) == (2000.0, 90000.0)
    assert got["servers"] == {"blog@file": {"up": 1, "down": 0}, "shop@file": {"up": 0, "down": 1}}
    assert got["reload_ok"] is True and got["reload_failures"] == 2.0
    # A failure after the last success: the configuration in use is not the latest one.
    worse = METRICS.replace("1.7937e+09", "1.7939e+09")
    assert traffic_ops.parse(worse)["reload_ok"] is False
    assert traffic_ops.parse("")["reload_ok"] is None


def test_odd_lines_are_skipped_and_the_number_of_services_is_bounded(monkeypatch):
    junk = (
        "garbage\n# comment\nname{broken 1\n"
        'traefik_service_requests_total{service="a",code="200"} NaN\n'
    )
    assert traffic_ops.parse(junk)["services"] == {}
    many = "\n".join(
        f'traefik_service_requests_total{{service="s{i}",code="200"}} 1' for i in range(300)
    )
    assert len(traffic_ops.parse(many)["services"]) == traffic_ops.MAX_SERVICES


def test_the_address_comes_from_the_operator_only_and_must_be_plain_http(monkeypatch):
    monkeypatch.delenv("ARMADOK_TRAEFIK_METRICS_URL", raising=False)
    assert traffic_ops.status({}) == {"configured": False}
    with pytest.raises(OperationError) as refused:
        traffic_ops.scrape({"url": "http://169.254.169.254/"})  # an argument is never used
    assert refused.value.code == "worker.metrics_not_configured"
    for bad in ("file:///etc/passwd", "ftp://x/metrics", "http://user:pw@host/metrics", "nonsense"):
        monkeypatch.setenv("ARMADOK_TRAEFIK_METRICS_URL", bad)
        assert traffic_ops.status({}) == {"configured": False}


# --- counters become figures -----------------------------------------------


def counters(**requests):
    return service.ServiceCounters(
        requests=requests, buckets={"0.1": 8.0, "+Inf": 10.0}, sum=1.0, count=10.0
    )


def test_the_first_reading_is_a_baseline_then_deltas_and_a_reset_counts_from_zero():
    first = counters(**{"2xx": 100.0})
    assert service.delta_of(first, None)["r2"] == 0.0  # history is not traffic
    later = service.ServiceCounters(
        requests={"2xx": 130.0, "5xx": 3.0},
        buckets={"0.1": 20.0, "+Inf": 30.0},
        sum=4.0,
        count=30.0,
    )
    delta = service.delta_of(later, service.counters_to_state(first))
    assert (delta["r2"], delta["r5"], delta["dur_count"]) == (30.0, 3.0, 20.0)
    assert delta["hist"] == {"0.1": 12.0, "+Inf": 20.0}
    restarted = counters(**{"2xx": 5.0})  # Traefik restarted: lower than before
    assert service.delta_of(restarted, service.counters_to_state(later))["r2"] == 5.0


def test_quantiles_are_interpolated_from_the_buckets():
    hist = {"0.1": 50.0, "0.5": 90.0, "1": 100.0, "+Inf": 100.0}
    assert service.quantile(hist, 0.5) == pytest.approx(0.1)
    assert 0.1 < service.quantile(hist, 0.75) < 0.5
    assert service.quantile({"0.1": 0.0, "+Inf": 0.0}, 0.5) is None
    assert service.quantile({"0.1": 5.0, "+Inf": 10.0}, 0.99) == 0.1  # beyond the last bound


# --- through the hub -------------------------------------------------------


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    routes._last_test.clear()
    service._busy.clear()
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False, local_node=True))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app


def answer_with(monkeypatch, *readings):
    queue = list(readings)

    async def call(app, reg, node_id, op, args, actor=None):
        assert op == "traffic.scrape"
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, str):
            from armadok.core.errors import ApiError

            raise ApiError(value, 409)
        return value

    monkeypatch.setattr(service, "call_operation", call)


def reading(ok, bad, total_count):
    return {
        "configured": True,
        "services": {
            "blog@file": {
                "requests": {"2xx": float(ok), "5xx": float(bad)},
                "buckets": {
                    "0.1": total_count * 0.8,
                    "1": float(total_count),
                    "+Inf": float(total_count),
                },
                "sum": total_count * 0.1,
                "count": float(total_count),
                "bytes_in": 10.0 * total_count,
                "bytes_out": 100.0 * total_count,
            }
        },
        "servers": {"blog@file": {"up": 1, "down": 0}},
        "reload_ok": True,
        "reload_failures": 0.0,
    }


def test_two_readings_give_the_traffic_between_them(box, monkeypatch):
    import asyncio

    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    answer_with(monkeypatch, reading(100, 0, 100), reading(190, 10, 200))
    assert asyncio.run(service.read_node(app, node)) is None  # baseline
    assert asyncio.run(service.read_node(app, node)) is None
    got = client.get(f"/api/v1/nodes/{node}/traffic?range=1h", headers=olga).json()
    blog = got["services"][0]
    assert blog["service"] == "blog@file" and blog["requests"] == 100
    assert blog["errors_5xx"] == 10 and blog["error_rate"] == 0.1
    assert blog["p95"] is not None and blog["bytes_out"] == 10000
    assert sum(point[0] for point in blog["series"]) == 100
    assert got["status"]["last_error"] is None and got["status"]["servers"]["blog@file"]["up"] == 1


def test_a_machine_without_metrics_says_why_and_rights_are_checked(box, monkeypatch):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    pat_id, pat = person(client, admin, "pat")
    answer_with(monkeypatch, "worker.metrics_not_configured")
    url = f"/api/v1/nodes/{node}/traffic"
    assert client.get(url, headers=pat).status_code == 404  # cannot see the machine
    done = client.post(url + "/test", headers=olga)
    assert done.json() == {"ok": False, "error": "worker.metrics_not_configured"}
    assert client.post(url + "/test", headers=olga).status_code == 429  # paced
    assert (
        client.get(url, headers=olga).json()["status"]["last_error"]
        == "worker.metrics_not_configured"
    )
    assert client.get(url + "?range=2y", headers=olga).status_code == 422


def test_old_figures_are_purged_and_a_hostile_answer_is_refused(box, monkeypatch):
    import asyncio

    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    answer_with(monkeypatch, {"services": {"x": {"requests": "many"}}})
    assert asyncio.run(service.read_node(app, node)) == "worker.invalid_answer"
    conn = app.state.settings.open_db()
    old = int(time.time()) - 40 * 86400
    conn.execute(
        "INSERT INTO traffic_buckets (node_id, service, ts, r2) VALUES (?, 's', ?, 5)", (node, old)
    )
    conn.commit()
    repository.purge_old(conn)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM traffic_buckets").fetchone()[0] == 0
    conn.close()


# --- a hostile worker ------------------------------------------------------


def test_hostile_figures_are_cleaned_before_they_can_poison_the_statistics():
    scrape = service.Scrape.model_validate(
        {
            "services": {
                "ok": {
                    "requests": {"2xx": 5, "evil": 9},
                    "buckets": {"abc": 1, "nan": 1, "1e999": 1, "0.5": 3, "+Inf": 5},
                    "count": 5,
                },
                "x" * 300: {"requests": {"2xx": 1}},
                "bad\nname": {"requests": {"2xx": 1}},
            },
            "servers": {"ok": {"up": 1, "weird": 3}, "bad\nname": {"up": 1}},
        }
    )
    assert list(scrape.services) == ["ok"]
    assert scrape.services["ok"].requests == {"2xx": 5.0}
    assert scrape.services["ok"].buckets == {"0.5": 3.0, "+Inf": 5.0}
    assert scrape.servers == {"ok": {"up": 1}}
    for bad in (float("inf"), float("nan"), -1.0, 1e300):
        with pytest.raises(Exception):  # noqa: B017, PT011
            service.Scrape.model_validate({"services": {"s": {"count": bad}}})
    with pytest.raises(Exception):  # noqa: B017, PT011
        service.Scrape.model_validate({"reload_failures": float("inf")})


def test_stored_junk_cannot_break_the_view(box):
    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    conn = app.state.settings.open_db()
    ts = int(time.time() // 300 * 300)
    conn.execute(
        "INSERT INTO traffic_buckets (node_id, service, ts, r2, hist) VALUES (?, 's', ?, 4, ?)",
        (node, ts, '{"abc": 3, "nan": 1, "0.5": 4, "+Inf": 4}'),
    )
    conn.execute(
        "INSERT INTO traffic_buckets (node_id, service, ts, r2, hist) VALUES (?, 't', ?, 1, 'x')",
        (node, ts),
    )
    conn.commit()
    conn.close()
    got = client.get(f"/api/v1/nodes/{node}/traffic?range=1h", headers=olga)
    assert got.status_code == 200
    names = {s["service"]: s for s in got.json()["services"]}
    assert names["s"]["requests"] == 4 and names["s"]["p50"] is not None
    assert names["t"]["p50"] is None


def test_a_machine_cannot_make_the_hub_keep_ever_more_service_names(box, monkeypatch):
    import asyncio

    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    monkeypatch.setattr(service, "MAX_SERVICE_NAMES", 3)

    def many(prefix, n):
        return {
            "configured": True,
            "services": {
                f"{prefix}{i}": {"requests": {"2xx": float(n)}, "count": float(n)} for i in range(5)
            },
        }

    answer_with(monkeypatch, many("a", 1), many("a", 10))
    asyncio.run(service.read_node(app, node))  # baseline
    asyncio.run(service.read_node(app, node))
    conn = app.state.settings.open_db()
    assert len(repository.service_names(conn, node)) == 3
    conn.close()


def test_a_second_reading_under_way_is_not_reported_as_a_success(box, monkeypatch):
    import asyncio

    client, admin, app = box
    _, olga, node = owner_with_node(client, admin)
    answer_with(monkeypatch, reading(1, 0, 1))
    service._busy.add(node)
    assert asyncio.run(service.read_node(app, node)) == "traffic.busy"
    service._busy.discard(node)


def test_a_bucket_line_without_a_bound_does_not_break_the_reading():
    text = (
        'traefik_service_request_duration_seconds_bucket{service="s"} 3\n'
        'traefik_service_requests_total{service="s",code="200"} 1\n'
    )
    assert traffic_ops.parse(text)["services"]["s"]["requests"] == {"2xx": 1.0}
