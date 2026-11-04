import io
import logging
import os
import re
import sqlite3
import stat
import tarfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from armadok import restore_state
from armadok.core.config import Settings, _whole
from armadok.core.crypto import generate_key
from armadok.core.errors import ApiError
from armadok.main import create_app
from armadok.modules.statebackup import archive, routes, watch
from armadok.modules.workers import identity

PASSWORD = "correct horse battery"
KEY = generate_key()


def make_app(tmp_path, caplog, **extra):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path / "data",
                cookie_secure=False,
                encryption_key=extra.pop("key", KEY),
                **extra,
            )
        )
    return app, re.search(r"\): (\S+)$", caplog.records[-1].getMessage()).group(1)


@pytest.fixture
def box(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(routes, "MIN_GAP_SECONDS", 0)
    app, code = make_app(tmp_path, caplog, state_backup_dir=tmp_path / "bk", state_backup_keep=3)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        yield client, login(client, "admin"), app, tmp_path


def login(client, name):
    token = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_a_backup_is_encrypted_and_holds_the_database_and_the_identity_key(box):
    client, admin, app, tmp = box
    identity.hub_private_key(app.state.settings)  # created on first use, e.g. by a worker
    name = client.post("/api/v1/statebackups", headers=admin).json()["name"]
    path = tmp / "bk" / name
    raw = path.read_bytes()
    assert b"SQLite format" not in raw and b"admin" not in raw  # nothing readable without the key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp / "bk").stat().st_mode) == 0o700
    members = archive.unseal(app.state.settings, raw)
    assert set(members) == {"manifest.json", "armadok.db", "hub_identity.key"}
    snapshot = tmp / "snap.db"
    snapshot.write_bytes(members["armadok.db"])
    names = [r[0] for r in sqlite3.connect(snapshot).execute("SELECT username FROM users")]
    assert "admin" in names


def test_the_wrong_key_cannot_open_a_backup(box):
    client, admin, app, tmp = box
    name = client.post("/api/v1/statebackups", headers=admin).json()["name"]
    other = Settings(data_dir=tmp / "data", encryption_key=generate_key())
    with pytest.raises(ApiError) as error:
        archive.unseal(other, (tmp / "bk" / name).read_bytes())
    assert error.value.code == "statebackup.wrong_key"


def test_without_an_encryption_key_no_backup_is_made(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(routes, "MIN_GAP_SECONDS", 0)
    app, code = make_app(tmp_path, caplog, key=None)
    with TestClient(app) as client:
        client.post(
            "/api/v1/setup", json={"install_code": code, "username": "admin", "password": PASSWORD}
        )
        admin = login(client, "admin")
        got = client.post("/api/v1/statebackups", headers=admin)
        assert (
            got.status_code == 409
            and got.json()["error"]["code"] == "statebackup.no_encryption_key"
        )
        assert client.get("/api/v1/statebackups", headers=admin).json()["ready"] is False
        assert watch.due(app.state.settings) is False


def test_only_the_newest_backups_are_kept(box):
    client, admin, app, tmp = box
    (tmp / "bk").mkdir(exist_ok=True)
    for second in range(5):
        stamp = f"20260101T0000{second:02d}Z"
        (tmp / "bk" / f"armadok-state-{stamp}.tar.gz.enc").write_bytes(b"x")
    client.post("/api/v1/statebackups", headers=admin)
    names = [b["name"] for b in client.get("/api/v1/statebackups", headers=admin).json()["backups"]]
    assert len(names) == 3 and names[0] > names[1]  # the new one, then the two newest old ones


def test_rights_and_keys(box):
    client, admin, app, tmp = box
    client.post(
        "/api/v1/users", headers=admin, json={"username": "eve", "password": PASSWORD}
    ).json()["id"]
    eve = login(client, "eve")
    assert client.get("/api/v1/statebackups", headers=eve).status_code == 403
    assert client.post("/api/v1/statebackups", headers=eve).status_code == 403
    assert client.get("/api/v1/statebackups").status_code == 401
    got = client.post(
        "/api/v1/apikeys", headers=admin, json={"name": "k", "rights": ["state_backup.manage"]}
    )
    assert got.status_code == 422 and got.json()["error"]["code"] == "apikeys.invalid_right"


def test_deleting_only_touches_listed_backups(box):
    client, admin, app, tmp = box
    name = client.post("/api/v1/statebackups", headers=admin).json()["name"]
    outside = tmp / "keep.txt"
    outside.write_text("x")
    (tmp / "bk" / "armadok-state-20250101T000000Z.tar.gz.enc").symlink_to(outside)
    assert (
        client.delete(
            "/api/v1/statebackups/armadok-state-20250101T000000Z.tar.gz.enc", headers=admin
        ).status_code
        == 404
    )
    assert client.delete("/api/v1/statebackups/..%2Fkeep.txt", headers=admin).status_code in (
        404,
        422,
    )
    assert outside.exists()
    assert client.delete(f"/api/v1/statebackups/{name}", headers=admin).status_code == 204
    assert "statebackup.delete" in client.get("/api/v1/audit", headers=admin).text


def test_downloading_needs_a_fresh_code_and_a_session(box):
    client, admin, app, tmp = box
    name = client.post("/api/v1/statebackups", headers=admin).json()["name"]
    # The account has no second factor set up yet: the download is refused.
    got = client.post(
        f"/api/v1/statebackups/{name}/download", headers=admin, json={"code": "123456"}
    )
    assert got.status_code == 403 and got.json()["error"]["code"] == "auth.2fa_required"
    assert (
        client.post(f"/api/v1/statebackups/{name}/download", json={"code": "1"}).status_code == 401
    )


def test_the_automatic_backup_runs_when_due_and_only_then(box, monkeypatch):
    client, admin, app, tmp = box
    settings = app.state.settings
    assert watch.due(settings) is True  # none yet
    name = client.portal.call(watch.run_once, app)
    assert name and watch.due(settings) is False
    old = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y%m%dT%H%M%SZ")
    (tmp / "bk" / name).rename(tmp / "bk" / f"armadok-state-{old}.tar.gz.enc")
    assert watch.due(settings) is True


def test_a_failed_automatic_backup_is_notified(box, monkeypatch):
    client, admin, app, tmp = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(watch, "emit", fake_emit)
    monkeypatch.setattr(archive, "create", lambda s: (_ for _ in ()).throw(OSError("disk full")))
    assert client.portal.call(watch.run_once, app) is None
    assert sent[0]["event"] == "statebackup.failed" and "disk full" not in str(sent)


def test_settings_fall_back_to_safe_values():
    assert _whole(None, 7, 1, 365) == 7
    assert _whole("abc", 7, 1, 365) == 7
    assert _whole("0", 7, 1, 365) == 7 and _whole("0", 24, 0, 720) == 0
    assert _whole("9999", 7, 1, 365) == 7


def test_the_restore_command_puts_data_back_and_keeps_what_was_there(box, caplog):
    client, admin, app, tmp = box
    identity.hub_private_key(app.state.settings)
    name = client.post("/api/v1/statebackups", headers=admin).json()["name"]
    settings = app.state.settings
    # Break the live database, then restore the backup over it.
    conn = sqlite3.connect(settings.db_path)
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()
    written = restore_state.restore(settings, tmp / "bk" / name)
    assert set(written) == {"armadok.db", "hub_identity.key"}
    names = [r[0] for r in sqlite3.connect(settings.db_path).execute("SELECT username FROM users")]
    assert "admin" in names
    assert any(".before-restore-" in p.name for p in settings.data_dir.iterdir())
    assert stat.S_IMODE(settings.db_path.stat().st_mode) == 0o600


def test_restore_ignores_anything_but_the_expected_files(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", encryption_key=KEY)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for member, data in (
            ("manifest.json", b'{"format": 1}'),
            ("armadok.db", b"db"),
            ("../../evil", b"x"),
            ("/etc/passwd", b"x"),
        ):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("hub_identity.key")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    from cryptography.fernet import Fernet

    sealed = Fernet(KEY.encode()).encrypt(buffer.getvalue())
    members = archive.unseal(settings, sealed)
    assert set(members) == {"manifest.json", "armadok.db"}


def test_a_backup_of_another_format_is_refused(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", encryption_key=KEY)
    from cryptography.fernet import Fernet

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = 13
        tar.addfile(info, io.BytesIO(b'{"format": 9}'))
    with pytest.raises(ApiError) as error:
        archive.unseal(settings, Fernet(KEY.encode()).encrypt(buffer.getvalue()))
    assert error.value.code == "statebackup.invalid"


def sealed(members: dict[str, bytes], key: str = KEY) -> bytes:
    from cryptography.fernet import Fernet

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return Fernet(key.encode()).encrypt(buffer.getvalue())


def real_db(settings) -> bytes:
    return settings.db_path.read_bytes()


def test_nothing_in_clear_is_ever_written_to_the_backup_folder(box):
    client, admin, app, tmp = box
    client.post("/api/v1/statebackups", headers=admin)
    names = os.listdir(tmp / "bk")
    assert len(names) == 1 and names[0].endswith(".tar.gz.enc")  # no scratch folder, no .part


def test_what_an_older_version_left_behind_is_swept(box):
    client, admin, app, tmp = box
    (tmp / "bk").mkdir(exist_ok=True)
    (tmp / "bk" / ".work-abc").mkdir()
    (tmp / "bk" / ".work-abc" / "armadok.db").write_bytes(b"clear")
    (tmp / "bk" / ".armadok-state-20250101T000000Z.tar.gz.enc.part").write_bytes(b"half")
    client.post("/api/v1/statebackups", headers=admin)
    assert [n for n in os.listdir(tmp / "bk") if n.startswith(".")] == []


def test_a_database_over_the_limit_is_refused(box, monkeypatch):
    client, admin, app, tmp = box
    monkeypatch.setattr(archive, "MAX_DB_BYTES", 10)
    got = client.post("/api/v1/statebackups", headers=admin)
    assert got.status_code == 413 and got.json()["error"]["code"] == "statebackup.too_large"


def test_a_malformed_encryption_key_is_an_error_code_not_a_crash(tmp_path):
    settings = Settings(data_dir=tmp_path, encryption_key="not-a-valid-fernet-key")
    with pytest.raises(ApiError) as error:
        archive.create(settings)
    assert error.value.code == "statebackup.no_encryption_key"


@pytest.mark.parametrize(
    "members",
    [
        {"manifest.json": b"[1, 2]", "armadok.db": b"x"},
        {"manifest.json": b"\xff\xfe not json", "armadok.db": b"x"},
        {"manifest.json": b'{"format": 1}'},
    ],
)
def test_odd_backups_are_refused_with_a_code(members):
    settings = Settings(data_dir=None, encryption_key=KEY)
    with pytest.raises(ApiError) as error:
        archive.unseal(settings, sealed(members))
    assert error.value.code == "statebackup.invalid"


def test_a_truncated_or_garbled_archive_is_refused_with_a_code(tmp_path):
    from cryptography.fernet import Fernet

    settings = Settings(data_dir=tmp_path, encryption_key=KEY)
    garbled = Fernet(KEY.encode()).encrypt(b"this is not a tar file at all")
    with pytest.raises(ApiError) as error:
        archive.unseal(settings, garbled)
    assert error.value.code == "statebackup.invalid"


def test_an_oversized_member_is_not_read(monkeypatch):
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", 5)
    settings = Settings(data_dir=None, encryption_key=KEY)
    with pytest.raises(ApiError):
        archive.unseal(
            settings, sealed({"manifest.json": b'{"format": 1}', "armadok.db": b"x" * 100})
        )


def test_a_planted_future_file_neither_blocks_the_backup_nor_pushes_real_ones_out(box):
    client, admin, app, tmp = box
    (tmp / "bk").mkdir(exist_ok=True)
    (tmp / "bk" / "armadok-state-29990101T000000Z.tar.gz.enc").write_bytes(b"planted")
    assert watch.due(app.state.settings) is True
    client.post("/api/v1/statebackups", headers=admin)
    names = [b["name"] for b in client.get("/api/v1/statebackups", headers=admin).json()["backups"]]
    assert all("2999" not in n for n in names) and len(names) == 1


def test_rotation_covers_every_file_even_beyond_the_listing_cap(box, monkeypatch):
    client, admin, app, tmp = box
    monkeypatch.setattr(archive, "MAX_LISTED", 2)
    (tmp / "bk").mkdir(exist_ok=True)
    for day in range(1, 8):
        (tmp / "bk" / f"armadok-state-2026010{day}T000000Z.tar.gz.enc").write_bytes(b"x")
    client.post("/api/v1/statebackups", headers=admin)
    assert len(os.listdir(tmp / "bk")) == 3  # keep=3, whatever the listing shows


def test_a_second_backup_at_the_same_moment_is_refused_not_doubled(box):
    client, admin, app, tmp = box
    assert archive._creating.acquire(blocking=False)
    try:
        got = client.post("/api/v1/statebackups", headers=admin)
        assert got.status_code == 409 and got.json()["error"]["code"] == "statebackup.too_soon"
    finally:
        archive._creating.release()


def test_a_failed_backup_does_not_make_the_next_one_wait(box, monkeypatch):
    client, admin, app, tmp = box
    routes._last_manual[0] = -1e9
    monkeypatch.setattr(routes, "MIN_GAP_SECONDS", 60)
    monkeypatch.setattr(archive, "MAX_DB_BYTES", 10)
    assert client.post("/api/v1/statebackups", headers=admin).status_code == 413
    monkeypatch.setattr(archive, "MAX_DB_BYTES", 10**9)
    assert client.post("/api/v1/statebackups", headers=admin).status_code == 201


def test_an_unexpected_failure_of_the_automatic_backup_is_notified(box, monkeypatch):
    client, admin, app, tmp = box
    sent = []

    async def fake_emit(_app, name, /, **payload):
        sent.append(payload)

    monkeypatch.setattr(watch, "emit", fake_emit)
    monkeypatch.setattr(
        archive, "create", lambda s: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    )
    client.portal.call(watch.run_once, app)
    assert sent and sent[0]["params"]["code"] == "statebackup.write_failed"
    sent.clear()
    monkeypatch.setattr(
        archive, "create", lambda s: (_ for _ in ()).throw(ApiError("statebackup.too_soon", 409))
    )
    client.portal.call(watch.run_once, app)
    assert sent == []  # a manual backup in progress is not an incident


def make_backup(box):
    client, admin, app, tmp = box
    identity.hub_private_key(app.state.settings)
    name = client.post("/api/v1/statebackups", headers=admin).json()["name"]
    return app.state.settings, tmp / "bk" / name


def stop_hub_files(settings):
    """What the data folder looks like with the hub stopped: no connection holds the database."""


def test_a_restore_is_refused_while_the_database_is_in_use_and_moves_nothing(box):
    client, admin, app, tmp = box
    settings, backup = make_backup(box)
    holder = sqlite3.connect(settings.db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        before = sorted(p.name for p in settings.data_dir.iterdir())
        with pytest.raises(ApiError) as error:
            restore_state.restore(settings, backup)
        assert error.value.code == "statebackup.hub_running"
        assert sorted(p.name for p in settings.data_dir.iterdir()) == before
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_backup_from_a_newer_version_is_refused_before_anything_changes(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", encryption_key=KEY)
    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES ('future-9999-from-a-newer-armadok')")
    conn.commit()
    conn.close()
    backup = tmp_path / "b.enc"
    backup.write_bytes(sealed({"manifest.json": b'{"format": 1}', "armadok.db": db.read_bytes()}))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "armadok.db").write_bytes(b"current")
    with pytest.raises(ApiError) as error:
        restore_state.restore(settings, backup)
    assert error.value.code == "statebackup.newer_version"
    assert (tmp_path / "d" / "armadok.db").read_bytes() == b"current"


def test_a_damaged_database_in_a_backup_is_refused(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", encryption_key=KEY)
    backup = tmp_path / "b.enc"
    backup.write_bytes(
        sealed({"manifest.json": b'{"format": 1}', "armadok.db": b"not a database" * 100})
    )
    with pytest.raises(ApiError) as error:
        restore_state.restore(settings, backup)
    assert error.value.code == "statebackup.corrupt"
    assert not (tmp_path / "d" / "armadok.db").exists()


def test_the_restore_command_reports_problems_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARMADOK_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("ARMADOK_ENCRYPTION_KEY", KEY)
    assert restore_state.main([str(tmp_path / "missing.enc"), "--yes"]) == 4
    monkeypatch.delenv("ARMADOK_ENCRYPTION_KEY")
    assert restore_state.main([str(tmp_path / "missing.enc"), "--yes"]) == 2
    assert KEY not in capsys.readouterr().err
