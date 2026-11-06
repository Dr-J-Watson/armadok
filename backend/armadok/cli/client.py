"""A small HTTP client for the Armadok API, and where the CLI keeps its settings.

The address and the API key come from the environment (`ARMADOK_URL`, `ARMADOK_API_KEY`) or
from a settings file readable by its owner only. The key is never printed, never put in a URL,
and a key file that other users can read is refused. Only `https://` is accepted for anything
but a loopback address, so a key never travels in clear text over a network.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 60  # per read; the whole exchange has its own deadline below
DEADLINE_SECONDS = 180
MAX_BODY_BYTES = 8 * 1024 * 1024
ENV_URL, ENV_KEY, ENV_FILE = "ARMADOK_URL", "ARMADOK_API_KEY", "ARMADOK_CLI_CONFIG"
ENV_PROXY = "ARMADOK_USE_PROXY"
# What a key looks like: visible ASCII only. Anything else (a stray line break, say) is refused
# before it can reach a header, where it would make the library print the whole key.
KEY = re.compile(r"[\x21-\x7e]{8,512}")


class CliError(Exception):
    """A problem to tell the user, with a stable code for scripts."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        """Keep the code, a human sentence and, for the API, the HTTP status."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def config_path() -> Path:
    """Return the settings file (overridable by `ARMADOK_CLI_CONFIG`)."""
    if os.environ.get(ENV_FILE):
        return Path(os.environ[ENV_FILE])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "armadok" / "cli.json"


def check_key(key: str) -> str:
    """Return `key` if it is plausible, else raise `CliError` (the key is never quoted)."""
    if not KEY.fullmatch(key):
        raise CliError(
            "cli.bad_key", "The API key is not valid (visible characters only, no spaces)."
        )
    return key


def save_config(url: str, key: str) -> Path:
    """Write the settings file, readable by its owner only, from the first byte.

    The file is made under a temporary name with exclusive creation and no symlink following,
    set to mode 0600 before anything is written, flushed, and only then moved into place: a
    crash leaves the old file, a planted link is not followed, and an existing 0644 file is
    replaced rather than written into.
    """
    check_url(url)
    check_key(key)
    path = config_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CliError(
                "cli.config_directory",
                f"{path.parent} must belong to you and not be writable by others.",
            )
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cli-", suffix=".tmp")
    except PermissionError:
        raise CliError(
            "cli.config_unwritable",
            f"Cannot write {path.parent}. Set {ENV_FILE} to a folder you own, or pass "
            f"{ENV_URL} and {ENV_KEY} in the environment "
            "(for a container: `docker exec --env-file`).",
        ) from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"url": url, "key": key}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def check_url(url: str) -> str:
    """Return `url` without a trailing slash if it is usable, else raise `CliError`."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise CliError("cli.bad_url", "The address is not valid.") from None
    loopback = host in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise CliError("cli.bad_url", "The address must look like https://armadok.example.com.")
    if parts.scheme == "http" and not loopback:
        raise CliError(
            "cli.insecure_url", "Use https:// : an API key must not travel in clear text."
        )
    if parts.query or parts.fragment:
        raise CliError("cli.bad_url", "The address must not contain a query or a fragment.")
    return url.rstrip("/")


def _read_settings_file(path: Path) -> dict[str, Any]:
    """Read the settings file: a plain file of ours that nobody else can read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError:
        raise CliError("cli.bad_config", f"{path} cannot be read (is it a link?).") from None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CliError("cli.config_permissions", f"{path} must be a file of yours.")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CliError(
                "cli.config_permissions", f"{path} must be readable by you only (chmod 600)."
            )
        try:
            data = json.loads(handle.read(65536))
        except ValueError:
            raise CliError("cli.bad_config", f"{path} is not a valid settings file.") from None
    return data if isinstance(data, dict) else {}


def load_settings() -> tuple[str, str]:
    """Return `(url, key)`: both from the environment, or both from the settings file.

    Never one of each: a forgotten address in the shell must not receive the stored key.
    """
    url, key = os.environ.get(ENV_URL), os.environ.get(ENV_KEY)
    if bool(url) != bool(key):
        raise CliError(
            "cli.partial_environment",
            f"Set both {ENV_URL} and {ENV_KEY}, or neither (use the saved login).",
        )
    if not url:
        data = _read_settings_file(config_path())
        url, key = data.get("url"), data.get("key")
    if not isinstance(url, str) or not isinstance(key, str) or not url or not key:
        raise CliError(
            "cli.not_configured",
            f"Run `armadok login` first, or set {ENV_URL} and {ENV_KEY}.",
        )
    return check_url(url), check_key(key)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the key must not be handed to another address."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


def call(method: str, path: str, body: Any = None, *, url: str, key: str) -> Any:
    """Call the API and return the decoded JSON (None for an empty answer). Raises `CliError`."""
    if not path.startswith("/") or "://" in path or ".." in path.split("?")[0].split("/"):
        raise CliError("cli.bad_path", "The path must start with / and stay inside the API.")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310  (the scheme was checked by check_url)
        f"{url}/api/v1{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    # Proxies are off unless asked for: the environment's proxy settings would otherwise carry
    # the key (in clear text, for an http address) to a node nobody chose.
    handlers: list[Any] = [_NoRedirect()]
    if os.environ.get(ENV_PROXY) != "1":
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            chunks, size = [], 0
            while chunk := response.read(65536):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise CliError("cli.too_large", "The answer is too large.")
                if time.monotonic() > deadline:
                    raise CliError("cli.timeout", "The server took too long to answer.")
                chunks.append(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as error:
        raise _api_error(error) from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise CliError("cli.tls_error", f"The certificate of {url} is not trusted.") from None
        raise CliError("cli.unreachable", f"Cannot reach {url}.") from None
    except (ValueError, http.client.HTTPException):
        # Never let the library's message through: it can quote the header, key included.
        raise CliError("cli.bad_request", "The request could not be built.") from None
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise CliError(
            "cli.bad_answer", "The server's answer is not JSON (check the address)."
        ) from None


def _api_error(error: urllib.error.HTTPError) -> CliError:
    code = "common.unknown"
    try:
        code = json.loads(error.read(65536))["error"]["code"]
    except (ValueError, KeyError, TypeError):
        pass
    if error.code in (301, 302, 303, 307, 308):
        return CliError(
            "cli.redirect", "The server redirected the request: check the address.", error.code
        )
    return CliError(
        str(code)[:80], f"The server answered {error.code} ({str(code)[:80]}).", error.code
    )
