import json
import logging
import re
import socket
import threading
import time

import pytest
import uvicorn

from armadok.cli import client, main
from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def server(tmp_path, caplog):
    """A real hub on a loopback port, with an admin and an API key for it."""
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(data_dir=tmp_path / "data", cookie_secure=False, encryption_key=generate_key())
        )
    code = re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    hub = uvicorn.Server(config)
    thread = threading.Thread(target=hub.run, daemon=True)
    thread.start()
    for _ in range(100):
        if hub.started:
            break
        time.sleep(0.05)
    import httpx

    base = f"http://127.0.0.1:{port}/api/v1"
    httpx.post(
        f"{base}/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
    )
    token = httpx.post(
        f"{base}/auth/login", json={"username": "admin", "password": PASSWORD}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    httpx.post(f"{base}/nodes", headers=headers, json={"name": "home", "type": "docker"})
    key = httpx.post(
        f"{base}/apikeys", headers=headers, json={"name": "cli", "rights": ["nodes.read"]}
    ).json()["key"]
    yield f"http://127.0.0.1:{port}", key
    hub.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMADOK_CLI_CONFIG", str(tmp_path / "cli.json"))
    monkeypatch.delenv("ARMADOK_URL", raising=False)
    monkeypatch.delenv("ARMADOK_API_KEY", raising=False)


def run(capsys, *argv):
    code = main.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_login_checks_the_key_and_keeps_it_for_its_owner_only(
    server, monkeypatch, capsys, tmp_path
):
    url, key = server
    monkeypatch.setenv("ARMADOK_API_KEY", key)
    code, out, err = run(capsys, "login", url)
    assert code == 0 and "Signed in as admin" in out and key not in out + err
    path = tmp_path / "cli.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    monkeypatch.delenv("ARMADOK_API_KEY")
    code, out, _ = run(capsys, "nodes")
    assert code == 0 and "home" in out and "docker" in out


def test_a_wrong_key_is_refused_and_not_kept(server, monkeypatch, capsys, tmp_path):
    url, _ = server
    monkeypatch.setenv("ARMADOK_API_KEY", "not-a-real-key")
    code, _, err = run(capsys, "login", url)
    assert code == 1 and "401" in err
    assert not (tmp_path / "cli.json").exists()


def test_json_output_and_whoami(server, monkeypatch, capsys):
    url, key = server
    monkeypatch.setenv("ARMADOK_URL", url)
    monkeypatch.setenv("ARMADOK_API_KEY", key)
    code, out, _ = run(capsys, "nodes", "--json")
    assert code == 0 and json.loads(out)[0]["name"] == "home"
    code, out, _ = run(capsys, "whoami", "--json")
    assert json.loads(out)["username"] == "admin"
    code, out, _ = run(capsys, "api", "get", "/nodes")
    assert code == 0 and "home" in out


def test_the_server_refusing_is_exit_code_1_and_a_missing_setup_is_2(server, monkeypatch, capsys):
    url, key = server
    code, _, err = run(capsys, "nodes")
    assert code == 2 and "armadok login" in err
    monkeypatch.setenv("ARMADOK_URL", url)
    monkeypatch.setenv("ARMADOK_API_KEY", key)
    code, _, err = run(capsys, "containers", "n" + "0" * 31)
    assert code == 1 and "404" in err


def test_a_key_is_never_sent_over_plain_http_to_another_machine():
    for url in ("http://armadok.example.com", "http://192.0.2.10:8000"):
        with pytest.raises(client.CliError) as error:
            client.check_url(url)
        assert error.value.code == "cli.insecure_url"
    assert client.check_url("https://armadok.example.com/") == "https://armadok.example.com"
    assert client.check_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for bad in ("ftp://x", "https://user:pw@example.com", "https://x.example.com/?a=1", "x", ""):
        with pytest.raises(client.CliError):
            client.check_url(bad)


def test_a_settings_file_others_can_read_is_refused(tmp_path):
    path = tmp_path / "cli.json"
    path.write_text(json.dumps({"url": "https://armadok.example.com", "key": "k" * 12}))
    path.chmod(0o644)
    with pytest.raises(client.CliError) as error:
        client.load_settings()
    assert error.value.code == "cli.config_permissions"
    path.chmod(0o600)
    assert client.load_settings() == ("https://armadok.example.com", "k" * 12)


@pytest.mark.parametrize(
    "path", ["nodes", "http://evil.example.com/x", "/../etc/passwd", "/a/../b"]
)
def test_paths_must_stay_inside_the_api(path):
    with pytest.raises(client.CliError) as error:
        client.call("GET", path, url="https://armadok.example.com", key="k")
    assert error.value.code == "cli.bad_path"


def test_a_redirect_is_not_followed_so_the_key_goes_nowhere_else():
    import http.server

    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/stolen")
            self.end_headers()

        def log_message(self, *a):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, redirector):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(client.CliError) as error:
            client.call(
                "GET",
                "/nodes",
                url=f"http://127.0.0.1:{redirector.server_address[1]}",
                key="secret",
            )
        assert error.value.code == "cli.redirect" and seen == []
    finally:
        target.shutdown()
        redirector.shutdown()


def test_an_unreachable_server_is_a_clear_error(capsys):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(client.CliError) as error:
        client.call("GET", "/nodes", url=f"http://127.0.0.1:{port}", key="k")
    assert error.value.code == "cli.unreachable"


def test_deploy_follows_the_task_and_reports_success_or_failure(monkeypatch, capsys):
    states = iter([{"state": "running"}, {"state": "success", "output": "done\n"}])
    sent = []

    def fake(method, path, body=None):
        sent.append((method, path, body))
        return {"task_id": "t1"} if method == "POST" else next(states)

    monkeypatch.setattr(main, "_api", fake)
    monkeypatch.setattr(main, "POLL_SECONDS", 0)
    code, out, _ = run(capsys, "deploy", "node1", "blog", "--pull")
    assert code == 0 and "up blog: success" in out
    assert sent[0] == (
        "POST",
        "/nodes/node1/projects/blog/actions",
        {"action": "up", "build": False, "pull": True},
    )
    monkeypatch.setattr(
        main,
        "_api",
        lambda m, p, b=None: (
            {"task_id": "t2"} if m == "POST" else {"state": "failed", "output": "boom"}
        ),
    )
    code, out, _ = run(capsys, "deploy", "node1", "blog", "--action", "restart")
    assert code == 3 and "failed" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["deploy", "n1", "../x"],
        ["deploy", "n/1", "blog"],
        ["projects", "a b"],
        ["logs", "n1", "x;rm"],
    ],
)
def test_names_are_checked_before_anything_is_sent(argv, monkeypatch, capsys):
    monkeypatch.setattr(main, "_api", lambda *a, **k: pytest.fail("must not call the API"))
    code, _, err = run(capsys, *argv)
    assert code == 2 and "not valid" in err


# --- review points ----------------------------------------------------------------------------


def test_a_key_with_a_line_break_is_refused_and_never_shown(monkeypatch, capsys):
    secret = "abcdefgh" + "\n" + "SECRETTAIL"
    monkeypatch.setenv("ARMADOK_URL", "https://armadok.example.com")
    monkeypatch.setenv("ARMADOK_API_KEY", secret)
    code, out, err = run(capsys, "nodes")
    assert code == 2 and "SECRETTAIL" not in out + err and "abcdefgh" not in out + err


def test_an_unexpected_error_prints_no_traceback_and_nothing_it_carries(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("Authorization: Bearer SUPERSECRETKEY")

    monkeypatch.setattr(main, "cmd_whoami", boom)
    parser_args = ["whoami"]
    code, out, err = run(capsys, *parser_args)
    assert code == 2 and "SUPERSECRETKEY" not in out + err and "Traceback" not in err
    code, out, err = run(capsys, "--debug", "whoami")
    assert "RuntimeError" in err and "SUPERSECRETKEY" not in err


def test_proxy_settings_do_not_carry_the_key_elsewhere(server, monkeypatch, capsys):
    url, key = server
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ARMADOK_URL", url)
    monkeypatch.setenv("ARMADOK_API_KEY", key)
    code, out, _ = run(capsys, "nodes")
    assert code == 0 and "home" in out  # went straight to the server, not through the proxy


def test_terminal_escape_sequences_from_the_server_are_neutralised():
    nasty = "ok\x1b]52;c;ZXZpbA==\x07\x1b[2J\x9b31m‮gnp.exe\x7f done\tfine\nnext"
    cleaned = main.clean(nasty)
    for bad in ("\x1b", "\x07", "\x9b", "‮", "\x7f"):
        assert bad not in cleaned
    assert "\t" in cleaned and "\n" in cleaned and cleaned.startswith("ok")
    assert "\x1b" not in main._dump({"name": "a\x1b[31mred"})


def test_logs_and_names_are_cleaned_before_printing(monkeypatch, capsys):
    monkeypatch.setattr(main, "_api", lambda *a, **k: {"output": "line\x1b[31m red\n"})
    code, out, _ = run(capsys, "logs", "n1", "web")
    assert code == 0 and "\x1b" not in out
    monkeypatch.setattr(
        main, "_api", lambda *a, **k: [{"id": "1", "name": "evil\x1b]0;x\x07", "type": "docker"}]
    )
    code, out, _ = run(capsys, "nodes")
    assert "\x1b" not in out and "\x07" not in out


def test_the_settings_file_replaces_a_loose_one_and_does_not_follow_links(tmp_path):
    path = tmp_path / "cli.json"
    path.write_text("old")
    path.chmod(0o644)
    client.save_config("https://armadok.example.com", "k" * 20)
    assert oct(path.stat().st_mode & 0o777) == "0o600" and "old" not in path.read_text()
    secret_target = tmp_path / "elsewhere"
    secret_target.write_text("untouched")
    path.unlink()
    path.symlink_to(secret_target)
    with pytest.raises(client.CliError):
        client.load_settings()  # a link is refused
    client.save_config("https://armadok.example.com", "k" * 20)  # replaced, not followed
    assert secret_target.read_text() == "untouched" and not path.is_symlink()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".cli-")]


def test_a_settings_directory_others_can_write_is_refused(tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    folder.mkdir()
    folder.chmod(0o777)
    monkeypatch.setenv("ARMADOK_CLI_CONFIG", str(folder / "cli.json"))
    with pytest.raises(client.CliError) as error:
        client.save_config("https://armadok.example.com", "k" * 20)
    assert error.value.code == "cli.config_directory"


def test_address_and_key_come_from_the_same_place(monkeypatch, tmp_path):
    client.save_config("https://stored.example.com", "s" * 20)
    monkeypatch.setenv("ARMADOK_URL", "https://forgotten.example.com")
    with pytest.raises(client.CliError) as error:
        client.load_settings()  # the stored key is not sent to an address from the shell
    assert error.value.code == "cli.partial_environment"
    monkeypatch.delenv("ARMADOK_URL")
    assert client.load_settings() == ("https://stored.example.com", "s" * 20)


def test_a_server_that_does_not_answer_json_is_a_clear_error(monkeypatch):
    import http.server

    class Html(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>login page</html>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Html)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(client.CliError) as error:
            client.call(
                "GET", "/nodes", url=f"http://127.0.0.1:{srv.server_address[1]}", key="k" * 12
            )
        assert error.value.code == "cli.bad_answer"
    finally:
        srv.shutdown()


def test_a_certificate_problem_has_its_own_code(monkeypatch):
    import ssl
    import urllib.error

    def refuse(self, request, timeout=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("bad"))

    monkeypatch.setattr("urllib.request.OpenerDirector.open", refuse)
    with pytest.raises(client.CliError) as error:
        client.call("GET", "/nodes", url="https://armadok.example.com", key="k" * 12)
    assert error.value.code == "cli.tls_error"


def test_a_body_can_come_from_a_file_or_stdin_and_must_be_json(monkeypatch, capsys, tmp_path):
    sent = []
    monkeypatch.setattr(main, "_api", lambda m, p, b=None: sent.append(b) or {"ok": True})
    body = tmp_path / "body.json"
    body.write_text('{"a": 1}')
    assert run(capsys, "api", "post", "/x", "--data", f"@{body}")[0] == 0
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"b": 2}'))
    assert run(capsys, "api", "post", "/x", "--data", "@-")[0] == 0
    assert sent == [{"a": 1}, {"b": 2}]
    code, _, err = run(capsys, "api", "post", "/x", "--data", "not json")
    assert code == 2 and "not valid JSON" in err
    code, _, err = run(capsys, "api", "post", "/x", "--data", "@/nonexistent/file")
    assert code == 2


def test_deploy_gives_the_task_handle_before_following_it(monkeypatch, capsys):
    states = iter([{"state": "success", "output": ""}])
    monkeypatch.setattr(
        main, "_api", lambda m, p, b=None: {"task_id": "t9"} if m == "POST" else next(states)
    )
    monkeypatch.setattr(main, "POLL_SECONDS", 0)
    code, out, err = run(capsys, "deploy", "n1", "blog")
    assert code == 0 and "task t9" in err
