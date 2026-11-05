"""The docker run converter: what it maps, what it warns about, and what it refuses."""

import logging
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from armadok.core.config import Settings
from armadok.main import create_app
from armadok.modules.convert import dockerrun
from tests.test_members_admin import PASSWORD, login


def service(command):
    result = dockerrun.convert(command)
    document = yaml.safe_load(result["compose"])
    return document, document["services"][result["service"]], result


def test_a_typical_command_becomes_a_service_with_its_ports_volumes_and_restart():
    document, svc, result = service(
        "docker run -d --name blog --restart unless-stopped -p 8080:80 -p 443:443 "
        "-v blog_data:/var/www -v /srv/site:/site:ro -e TZ=Europe/Paris --network proxy "
        "-w /app -u 1000:1000 nginx:1.27 nginx -g 'daemon off;'"
    )
    assert result["service"] == "blog"
    assert svc["image"] == "nginx:1.27" and svc["container_name"] == "blog"
    assert svc["ports"] == ["8080:80", "443:443"]
    assert svc["volumes"] == ["blog_data:/var/www", "/srv/site:/site:ro"]
    assert document["volumes"] == {"blog_data": {}}  # a named volume is declared, a path is not
    assert svc["environment"] == {"TZ": "Europe/Paris"}
    assert svc["restart"] == "unless-stopped" and svc["working_dir"] == "/app"
    assert svc["networks"] == ["proxy"] and document["networks"] == {"proxy": {"external": True}}
    assert svc["command"] == ["nginx", "-g", "daemon off;"]
    assert result["warnings"] == []


def test_combined_short_flags_attached_values_and_line_continuations_are_read():
    _, svc, result = service(
        "docker run -dit -p8080:80 --name=app \\\n   -e FOO=bar \\\n"
        "  --memory 512m --cpus 1.5 alpine sleep 1d"
    )
    assert svc["tty"] is True and svc["stdin_open"] is True
    assert svc["ports"] == ["8080:80"] and svc["container_name"] == "app"
    assert svc["mem_limit"] == "512m" and svc["cpus"] == "1.5"
    assert svc["command"] == ["sleep", "1d"]


def test_secret_looking_values_go_to_an_env_text_and_the_compose_refers_to_them():
    token = "tok" + "en-value"
    _, svc, result = service(
        f"docker run -d -e DB_PASSWORD=hunter-two -e API_TOKEN={token} -e PLAIN=ok app:1"
    )
    assert svc["environment"] == {
        "DB_PASSWORD": "${DB_PASSWORD}",
        "API_TOKEN": "${API_TOKEN}",
        "PLAIN": "ok",
    }
    assert result["env"] == f"DB_PASSWORD=hunter-two\nAPI_TOKEN={token}\n"
    assert "hunter-two" not in result["compose"] and token not in result["compose"]


def test_health_limits_capabilities_labels_and_modes():
    _, svc, _ = service(
        "docker run -d --privileged --read-only --init --cap-add NET_ADMIN --device /dev/ttyUSB0 "
        "--label traefik.enable=true -l a=b --health-cmd 'curl -f http://localhost/' "
        "--health-interval 30s --health-retries 3 --network host --entrypoint /bin/sh img"
    )
    assert svc["privileged"] is True and svc["read_only"] is True and svc["init"] is True
    assert svc["cap_add"] == ["NET_ADMIN"] and svc["devices"] == ["/dev/ttyUSB0"]
    assert svc["labels"] == {"traefik.enable": "true", "a": "b"}
    assert svc["healthcheck"] == {
        "test": ["CMD-SHELL", "curl -f http://localhost/"],
        "interval": "30s",
        "retries": 3,
    }
    assert svc["network_mode"] == "host" and "networks" not in svc
    assert svc["entrypoint"] == ["/bin/sh"]


def test_what_cannot_be_converted_is_listed_not_guessed():
    result = dockerrun.convert("docker run --rm --weird-flag -p 80:80 nginx")
    codes = [(w["code"], w["detail"]) for w in result["warnings"]]
    assert ("convert.unsupported_flag", "--weird-flag") in codes
    assert ("convert.rm", "") in codes and ("convert.foreground", "") in codes


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("docker ps", "convert.not_run"),
        ("docker run -d", "convert.no_image"),
        ("docker run -d -e X=$(cat /etc/passwd) img", "convert.substitution"),
        ("docker run -d -e X=`id` img", "convert.substitution"),
        ("docker run -e 'unterminated", "convert.unparsable"),
        ("x" * (dockerrun.MAX_COMMAND + 1), "convert.too_long"),
    ],
)
def test_commands_that_cannot_be_read_are_refused_with_a_code(command, code):
    with pytest.raises(ValueError) as caught:
        dockerrun.convert(command)
    assert str(caught.value) == code


def test_the_yaml_is_made_by_the_library_so_a_hostile_value_cannot_add_fields():
    document, svc, _ = service("docker run -d -e \"A=1\\nevil: yes\" -l 'x=y\\nz: 1' img")
    assert list(document) == ["services"]
    assert "evil" not in document["services"]["img"]
    assert svc["environment"]["A"] == "1\\nevil: yes"


@pytest.fixture
def box(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(Settings(data_dir=tmp_path, cookie_secure=False, local_node=True))
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin")


def test_the_endpoint_needs_an_account_and_returns_errors_as_codes(box):
    client, admin = box
    url = "/api/v1/convert/docker-run"
    assert client.post(url, json={"command": "docker run img"}).status_code == 401
    ok = client.post(url, headers=admin, json={"command": "docker run -d -p 80:80 nginx"})
    assert ok.status_code == 200 and "nginx" in ok.json()["compose"]
    bad = client.post(url, headers=admin, json={"command": "docker ps"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "convert.not_run"
    assert client.post(url, headers=admin, json={"command": ""}).status_code == 422
    assert client.post(url, headers=admin, json={"command": "x" * 9000}).status_code == 422
