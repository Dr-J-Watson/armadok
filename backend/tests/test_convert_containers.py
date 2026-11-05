"""Converting running containers into a Compose file (worker reading + hub conversion)."""

import json
import threading
from http.server import BaseHTTPRequestHandler

import pytest
import yaml

import tests.test_docker as base
from armadok.modules.convert import containers
from armadok.worker import inspect_ops
from armadok.worker.ops import OperationError
from tests.test_docker import PASSWORD, UnixServer, connect_worker, live  # noqa: F401

IMAGE_ID = "sha256:" + "b" * 64
SECRET_VALUE = "pa" + "ss" + "$word\nline2"  # a dollar sign and a line break, on purpose
CONTAINER = {
    "Id": "a" * 64,
    "Name": "/Web App",
    "Image": IMAGE_ID,
    "Config": {
        "Image": "nginx:1.27",
        "Hostname": "a" * 12,
        "Env": ["PATH=/usr/bin", "MODE=prod", "DB_PASSWORD=" + SECRET_VALUE, "PRICE=$5"],
        "Cmd": ["nginx", "-g", "daemon off;"],
        "Entrypoint": None,
        "WorkingDir": "/app",
        "User": "",
        "Labels": {
            "com.docker.compose.project": "old",
            "maintainer": "image-author",
            "traefik.enable": "true",
            "traefik.http.routers.w.rule": "Host(`a.example.com`)",
        },
        "Tty": False,
        "OpenStdin": True,
        "Healthcheck": {"Test": ["CMD", "true"], "Interval": 30 * 10**9, "Retries": 3},
    },
    "HostConfig": {
        "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]},
        "RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3},
        "NetworkMode": "demo_net",
        "Privileged": False,
        "CapAdd": ["NET_ADMIN"],
        "Memory": 268435456,
        "NanoCpus": 500000000,
        "LogConfig": {"Type": "json-file"},
        "ShmSize": 67108864,
        "ReadonlyRootfs": True,
    },
    "Mounts": [
        {"Type": "volume", "Name": "data", "Source": "/x", "Destination": "/data", "RW": True},
        {"Type": "volume", "Name": "f" * 64, "Source": "/y", "Destination": "/tmp2", "RW": True},
        {"Type": "bind", "Source": "/srv/site", "Destination": "/site", "RW": False},
    ],
    "NetworkSettings": {"Networks": {"demo_net": {}, "bridge": {}}},
}
IMAGE = {
    "Config": {
        "Env": ["PATH=/usr/bin"],
        "Cmd": ["nginx", "-g", "daemon off;"],
        "Labels": {"maintainer": "image-author"},
        "WorkingDir": "",
    }
}


class Daemon(BaseHTTPRequestHandler):
    """A stand-in for the Docker daemon that knows one container and its image."""

    def do_GET(self):
        body = {
            "/containers/" + "a" * 64 + "/json": CONTAINER,
            f"/images/{IMAGE_ID}/json": IMAGE,
        }.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body if body is not None else {}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    path = str(tmp_path / "docker.sock")
    server = UnixServer(path, Daemon)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARMADOK_DOCKER_SOCKET", path)
    yield path
    server.shutdown()


@pytest.fixture(autouse=True)
def with_convert(monkeypatch):
    monkeypatch.setattr(base, "MODULES", (*base.MODULES, "convert", "audit"))


def test_the_worker_keeps_only_what_the_person_chose(fake_docker):
    out = inspect_ops.containers_inspect({"ids": ["a" * 64]})["containers"][0]
    assert out["env"] == ["MODE=prod", "DB_PASSWORD=" + SECRET_VALUE, "PRICE=$5"]
    assert out["command"] == [] and out["labels"] == {
        "traefik.enable": "true",
        "traefik.http.routers.w.rule": "Host(`a.example.com`)",
    }
    assert out["compose_project"] == "old" and out["hostname"] == ""
    assert out["ports"] == ["8080:80/tcp"] and out["working_dir"] == "/app"
    assert out["healthcheck"]["retries"] == 3


@pytest.mark.parametrize("ids", [[], ["a" * 64] * 21, ["../x"], "a" * 64, [3]])
def test_the_worker_refuses_bad_id_lists(fake_docker, ids):
    with pytest.raises(OperationError):
        inspect_ops.containers_inspect({"ids": ids})


def converted(fake_docker):
    answer = containers.Answer.model_validate(inspect_ops.containers_inspect({"ids": ["a" * 64]}))
    return containers.convert(answer.containers)


def test_the_compose_file_has_the_settings_and_the_secrets_go_to_env(fake_docker):
    result = converted(fake_docker)
    doc = yaml.safe_load(result["compose"])
    service = doc["services"]["web-app"]
    assert service["image"] == "nginx:1.27" and service["container_name"] == "Web App"
    assert service["ports"] == ["8080:80/tcp"] and service["restart"] == "on-failure:3"
    assert service["volumes"] == ["data:/data", "/tmp2", "/srv/site:/site:ro"]
    assert service["networks"] == ["demo_net"] and doc["networks"] == {
        "demo_net": {"external": True}
    }
    assert doc["volumes"] == {"data": {"external": True}}
    assert service["cap_add"] == ["NET_ADMIN"] and service["read_only"] and service["stdin_open"]
    assert service["mem_limit"] == 268435456 and service["cpus"] == 0.5
    assert service["healthcheck"] == {"test": ["CMD", "true"], "interval": "30s", "retries": 3}
    # A dollar sign must reach the container as it was, and a secret stays out of the file.
    assert service["environment"]["PRICE"] == "$$5"  # as read back by Compose: a literal $5
    assert service["environment"]["DB_PASSWORD"] == "${DB_PASSWORD}"
    assert SECRET_VALUE not in result["compose"] and "line2" not in result["compose"]
    assert result["env"].count("\n") == 1 and result["env"].startswith('DB_PASSWORD="')
    assert [w["code"] for w in result["warnings"]] == [
        "convert.already_compose",
        "convert.env_inline",
    ]


def test_several_containers_get_distinct_names_and_prefixed_secrets():
    one = containers.Described(name="db", image="x", env=["API_KEY=" + "k" * 8])
    two = containers.Described(name="db", image="y", env=["API_KEY=" + "j" * 8])
    result = containers.convert([one, two])
    doc = yaml.safe_load(result["compose"])
    assert list(doc["services"]) == ["db", "db-2"]
    assert doc["services"]["db-2"]["environment"]["API_KEY"] == "${DB_2_API_KEY}"
    assert result["env"].splitlines() == ["DB_API_KEY=kkkkkkkk", "DB_2_API_KEY=jjjjjjjj"]


def test_every_field_escapes_dollars_and_secrets_never_collide_or_stay_inline():
    named = {"name": "a-b", "image": "x$y", "working_dir": "/w/${HOME}"}
    one = containers.Described(**named, env=["DATABASE_URL=postgres://u:pw@db/x", "FLAG", "MODE=1"])
    two = containers.Described(name="a_b", image="y", env=["AUTH_X=" + "q" * 6])
    result = containers.convert([one, two])
    doc = yaml.safe_load(result["compose"])
    first = doc["services"]["a-b"]
    assert first["working_dir"] == "/w/$${HOME}" and first["image"] == "x$$y"
    assert first["environment"] == {
        "DATABASE_URL": "${A_B_DATABASE_URL}",
        "FLAG": None,
        "MODE": "1",
    }
    assert doc["services"]["a_b"]["environment"]["AUTH_X"] == "${A_B_2_AUTH_X}"
    assert "pw@" not in result["compose"]
    codes = [w["code"] for w in result["warnings"]]
    assert "convert.env_inline" in codes


def test_secret_names_never_collide_and_a_worker_cannot_forge_a_reference():
    one = containers.Described(name="a", image="x", env=["B_PASSWORD=" + "1" * 6])
    two = containers.Described(name="a-b", image="y", env=["PASSWORD=" + "2" * 6])
    odd = containers.Described(
        name="c", image="@@secret:HOME@@", labels={"k": "@@secret:X@@"}, env=["my-token=" + "3" * 6]
    )
    result = containers.convert([one, two, odd])
    assert len(result["env"].splitlines()) == 2 and "111111" in result["env"]
    doc = yaml.safe_load(result["compose"])
    refs = {
        s["environment"].get("B_PASSWORD") or s["environment"].get("PASSWORD")
        for s in list(doc["services"].values())[:2]
    }
    assert len(refs) == 2
    assert doc["services"]["c"]["image"] == "@@secret:HOME@@"  # plain text, not a variable
    assert "convert.secret_inline" in [w["code"] for w in result["warnings"]]


def test_a_hostile_answer_is_refused():
    with pytest.raises(ValueError):
        containers.Answer.model_validate({"containers": [{"name": "x" * 400, "image": "i"}]})
    with pytest.raises(ValueError):
        containers.Answer.model_validate(
            {"containers": [{"name": "x", "image": "i", "ports": ["1"] * 101}]}
        )


def test_through_the_hub_needs_a_sensitive_right_and_leaves_no_values_in_the_log(live):  # noqa: F811
    client, headers, port, tmp_path = live
    node, stop = connect_worker(client, headers, port, tmp_path, "docker")
    url = f"/api/v1/nodes/{node}/convert/containers"
    done = client.post(url, headers=headers, json={"ids": ["a" * 64]})
    assert done.status_code == 200 and "web-app" in done.json()["compose"]
    assert "convert.privileged" not in json.dumps(done.json())
    assert client.post(url, headers=headers, json={"ids": ["zz"]}).status_code == 422
    # An API key can never hold the right, even one made by an administrator.
    made = client.post(
        "/api/v1/apikeys", headers=headers, json={"name": "k", "rights": ["convert.containers"]}
    )
    if made.status_code < 300:
        key = {"Authorization": f"Bearer {made.json()['key']}"}
        assert client.post(url, headers=key, json={"ids": ["a" * 64]}).status_code in (403, 404)
    log = client.get("/api/v1/audit", headers=headers).text
    assert "convert.containers" in log and "line2" not in log and "DB_PASSWORD" not in log
    stop.set()
