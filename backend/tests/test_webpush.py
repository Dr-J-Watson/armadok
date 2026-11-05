"""Browser push: the standards' own test vector, a round trip, and delivery through a channel."""

import base64
import logging
import re

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from armadok.core import outbound, webpush
from armadok.core.config import Settings
from armadok.core.crypto import generate_key
from armadok.main import create_app
from armadok.modules.notifications import delivery, service
from tests.test_members_admin import PASSWORD, login


def unb(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- the cryptography --------------------------------------------


def test_rfc_8291_appendix_a_vector_is_reproduced_exactly():
    sender = ec.derive_private_key(
        int.from_bytes(unb("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1()
    )
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        unb(
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
        ),
        unb("BTBZMqHH6r4Tts7J_aSIgg"),
        sender=sender,
        salt=unb("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert webpush.b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6"
        "TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qul"
        "cy4a-fN"
    )


def decrypt_as_browser(body, ua_private, auth_secret):
    """The browser's side of RFC 8291, written apart from `encrypt`, to prove they agree."""
    salt, size, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, sealed = body[21 : 21 + idlen], body[21 + idlen :]
    assert size == 4096
    ua_public = webpush._point(ua_private.public_key())
    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    )

    def hkdf(s, k, info, n):
        return HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=info).derive(k)

    ikm = hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    key = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(key).decrypt(nonce, sealed, None).rstrip(b"\x00")  # zeros are padding
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_a_message_only_its_browser_can_read_and_two_messages_never_look_alike():
    browser = ec.generate_private_key(ec.SECP256R1())
    point, auth = webpush._point(browser.public_key()), b"0123456789abcdef"
    first = webpush.encrypt("héllo ✓".encode(), point, auth)
    second = webpush.encrypt("héllo ✓".encode(), point, auth)
    assert first != second  # a fresh key and salt each time
    assert decrypt_as_browser(first, browser, auth) == "héllo ✓".encode()
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(Exception):  # noqa: B017, PT011
        decrypt_as_browser(first, other, auth)


def test_oversized_messages_and_broken_keys_are_refused():
    browser = ec.generate_private_key(ec.SECP256R1())
    point, auth = webpush._point(browser.public_key()), b"0123456789abcdef"
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), point, auth)
    for bad_point, bad_auth in (
        (point[:-1], auth),
        (b"\x02" + point[1:], auth),
        (point, auth[:-1]),
    ):
        with pytest.raises(ValueError):
            webpush.encrypt(b"hi", bad_point, bad_auth)


def test_the_vapid_token_names_the_push_service_and_verifies_with_the_public_key():
    key = webpush.new_vapid_key()
    header = webpush.vapid_header(
        key, "https://push.example.com/send/abc?x=1", "https://example.com", now=1000
    )
    token = header.split("t=")[1].split(",")[0]
    head, body, signature = token.split(".")
    claims = __import__("json").loads(unb(body))
    assert claims == {
        "aud": "https://push.example.com",
        "exp": 1000 + 12 * 3600,
        "sub": "https://example.com",
    }
    raw = unb(signature)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    assert header.endswith("k=" + webpush.vapid_public(key))
    assert (
        webpush.load_vapid_key(webpush.vapid_private_pem(key)).private_numbers()
        == key.private_numbers()
    )
    with pytest.raises(ValueError):
        webpush.load_vapid_key("not a key")


# --- through the notifications -----------------------------------


@pytest.fixture
def box(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="armadok.setup"):
        app = create_app(
            Settings(
                data_dir=tmp_path,
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
        yield client, login(client, "admin"), app


def subscription():
    browser = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    return (
        browser,
        auth,
        {
            "endpoint": "https://push.example.com/send/abc",
            "p256dh": webpush.b64u(webpush._point(browser.public_key())),
            "auth": webpush.b64u(auth),
        },
    )


def test_the_server_gives_the_same_public_key_every_time(box):
    client, admin, _ = box
    first = client.get("/api/v1/notifications/push-key", headers=admin).json()["public_key"]
    again = client.get("/api/v1/notifications/push-key", headers=admin).json()["public_key"]
    assert first == again and len(unb(first)) == 65
    assert client.get("/api/v1/notifications/push-key").status_code == 401


def test_a_test_message_reaches_the_browser_encrypted_and_signed(box, monkeypatch):
    client, admin, app = box
    browser, auth, settings = subscription()
    made = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "webpush", "name": "Laptop", "settings": settings},
    )
    assert made.status_code == 201 and made.json()["needs_confirmation"] is False
    sent = []

    async def post(url, content, headers, allow_private=False):
        sent.append((url, content, headers))
        return 201

    monkeypatch.setattr(outbound, "post", post)
    channel = client.get("/api/v1/notifications/channels", headers=admin).json()[0]
    assert "endpoint" not in str(channel) and "p256dh" not in str(channel)  # never sent back
    assert (
        client.post(
            f"/api/v1/notifications/channels/{channel['id']}/test", headers=admin
        ).status_code
        == 200
    )
    url, content, headers = sent[0]
    assert url == settings["endpoint"] and headers["Content-Encoding"] == "aes128gcm"
    public = client.get("/api/v1/notifications/push-key", headers=admin).json()["public_key"]
    assert headers["Authorization"].endswith("k=" + public)
    message = __import__("json").loads(decrypt_as_browser(content, browser, auth))
    assert message["title"] == "Armadok test"


def test_a_browser_that_unsubscribed_is_told_apart(box, monkeypatch):
    client, admin, app = box
    _, _, settings = subscription()
    client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "webpush", "name": "Laptop", "settings": settings},
    )

    async def gone(url, content, headers, allow_private=False):
        return 410

    monkeypatch.setattr(outbound, "post", gone)
    channel = client.get("/api/v1/notifications/channels", headers=admin).json()[0]
    got = client.post(f"/api/v1/notifications/channels/{channel['id']}/test", headers=admin)
    assert got.status_code == 410 and got.json()["error"]["code"] == "notifications.push_gone"
    status = client.get("/api/v1/notifications/channels", headers=admin).json()[0]["last_status"]
    assert status == "notifications.push_gone"


@pytest.mark.parametrize(
    "bad",
    [
        {"endpoint": "http://push.example.com/x"},  # not https
        {"endpoint": "https://push.example.com/x\nHost: evil"},
        {"p256dh": "short"},
        {"auth": "short"},
        {"p256dh": "A" * 87},  # right length, not a P-256 point
    ],
)
def test_a_broken_subscription_is_refused(box, bad):
    client, admin, _ = box
    _, _, settings = subscription()
    got = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "webpush", "name": "x", "settings": {**settings, **bad}},
    )
    assert got.status_code == 422


def test_a_push_to_a_private_address_is_stopped_by_the_outbound_guard(box):
    import asyncio

    _, _, settings = subscription()
    config = delivery.PushConfig.model_validate(
        {**settings, "endpoint": "https://127.0.0.1/x", "kind": "webpush"}
    ).model_dump()
    key = webpush.new_vapid_key()
    with pytest.raises(outbound.OutboundError):
        asyncio.run(
            delivery.send(
                config,
                event="test",
                severity="info",
                title="t",
                body="b",
                node_id=None,
                allow_private=False,
                push_key=key,
                push_contact="https://example.com",
            )
        )


# --- review follow-ups -------------------------------------------


def test_every_message_has_the_same_length_and_emoji_never_overflow():
    browser = ec.generate_private_key(ec.SECP256R1())
    point, auth = webpush._point(browser.public_key()), b"0123456789abcdef"
    short = webpush.encrypt(b"hi", point, auth, pad_to=delivery.PUSH_PAD_BYTES)
    longer = webpush.encrypt(b"x" * 500, point, auth, pad_to=delivery.PUSH_PAD_BYTES)
    assert len(short) == len(longer)
    assert decrypt_as_browser(short, browser, auth) == b"hi"
    heavy = delivery._push_message("😀" * 200, "😀" * 400, "e" * 100)
    assert len(heavy) <= webpush.MAX_PLAINTEXT
    sealed = webpush.encrypt(heavy, point, auth, pad_to=delivery.PUSH_PAD_BYTES)
    assert decrypt_as_browser(sealed, browser, auth) == heavy
    assert b"\\u" not in heavy  # real UTF-8, not 12-byte escapes


def test_a_key_that_is_not_on_the_curve_is_refused_at_creation(box):
    client, admin, _ = box
    _, _, settings = subscription()
    off_curve = webpush.b64u(b"\x04" + b"\x01" * 64)
    got = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "webpush", "name": "x", "settings": {**settings, "p256dh": off_curve}},
    )
    assert got.status_code == 422


def test_a_browser_gone_twice_in_a_row_loses_its_channel(box):
    client, admin, app = box
    _, _, settings = subscription()
    client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={"kind": "webpush", "name": "Laptop", "settings": settings},
    )
    channel = client.get("/api/v1/notifications/channels", headers=admin).json()[0]["id"]
    service._record_status(app, channel, "notifications.push_gone")
    assert client.get("/api/v1/notifications/channels", headers=admin).json()[0]["last_status"] == (
        "notifications.push_gone"
    )
    service._record_status(app, channel, "notifications.push_gone")
    assert client.get("/api/v1/notifications/channels", headers=admin).json() == []


def test_the_exact_gone_status_is_kept_vague_when_private_addresses_are_allowed(box, monkeypatch):
    import asyncio

    _, _, settings = subscription()
    config = delivery.PushConfig.model_validate({**settings, "kind": "webpush"}).model_dump()

    async def gone(url, content, headers, allow_private=False):
        return 410

    monkeypatch.setattr(outbound, "post", gone)
    key = webpush.new_vapid_key()

    def attempt(allow):
        return asyncio.run(
            delivery.send(
                config,
                event="e",
                severity="info",
                title="t",
                body="b",
                node_id=None,
                allow_private=allow,
                push_key=key,
                push_contact="https://example.com",
            )
        )

    with pytest.raises(outbound.OutboundError) as strict:
        attempt(False)
    assert strict.value.code == "notifications.push_gone"
    with pytest.raises(outbound.OutboundError) as vague:
        attempt(True)
    assert vague.value.code == "outbound.rejected"


def test_one_account_cannot_aim_many_channels_at_one_host(box):
    client, admin, _ = box
    for i in range(5):
        _, _, settings = subscription()
        made = client.post(
            "/api/v1/notifications/channels",
            headers=admin,
            json={
                "kind": "webpush",
                "name": f"b{i}",
                "settings": {**settings, "endpoint": f"https://push.example.com/{i}"},
            },
        )
        assert made.status_code == 201
    _, _, settings = subscription()
    sixth = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "webpush",
            "name": "b6",
            "settings": {**settings, "endpoint": "https://push.example.com/6"},
        },
    )
    assert sixth.status_code == 409
    other = client.post(
        "/api/v1/notifications/channels",
        headers=admin,
        json={
            "kind": "webpush",
            "name": "o",
            "settings": {**settings, "endpoint": "https://other.example.org/1"},
        },
    )
    assert other.status_code == 201
