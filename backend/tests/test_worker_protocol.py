import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from armadok.worker_protocol import channel as ch


def pair(worker_id="w1"):
    hub_key = Ed25519PrivateKey.generate()
    client = ch.ClientHandshake(ch.public_key_bytes(hub_key), worker_id)
    server = ch.ServerHandshake(hub_key)
    server_hello, hub_channel = server.respond(client.hello)
    worker_channel = client.finish(server_hello)
    return client, server, worker_channel, hub_channel


def test_both_sides_agree_and_talk_both_ways():
    client, server, worker, hub = pair()
    assert client.transcript == server.transcript and server.worker_id == "w1"
    assert hub.open(worker.seal(b"hello hub")) == b"hello hub"
    assert worker.open(hub.seal(b"hello worker")) == b"hello worker"


def test_hub_with_another_key_is_rejected_by_the_pinned_key():
    pinned = Ed25519PrivateKey.generate()
    impostor = Ed25519PrivateKey.generate()
    client = ch.ClientHandshake(ch.public_key_bytes(pinned))
    server_hello, _ = ch.ServerHandshake(impostor).respond(client.hello)
    with pytest.raises(ch.ProtocolError):
        client.finish(server_hello)


def test_replayed_dropped_reordered_and_altered_frames_are_rejected():
    _, _, worker, hub = pair()
    _, second = worker.seal(b"one"), worker.seal(b"two")
    with pytest.raises(ch.ProtocolError):  # reordered / dropped: counter mismatch
        hub.open(second)
    _, _, worker, hub = pair()
    frame = worker.seal(b"one")
    assert hub.open(frame) == b"one"
    with pytest.raises(ch.ProtocolError):  # replay
        hub.open(frame)
    _, _, worker, hub = pair()
    tampered = bytearray(worker.seal(b"one"))
    tampered[0] ^= 1
    with pytest.raises(ch.ProtocolError):  # modified
        hub.open(bytes(tampered))


def test_each_connection_has_fresh_keys():
    _, _, w1, _ = pair()
    _, _, _, h2 = pair()
    with pytest.raises(ch.ProtocolError):
        h2.open(w1.seal(b"cross-connection"))


def test_directions_use_different_keys():
    _, _, worker, hub = pair()
    frame = worker.seal(b"x")
    with pytest.raises(ch.ProtocolError):  # a worker frame reflected back to the worker
        worker.open(frame)


def test_malformed_hello_and_oversized_frames():
    server = ch.ServerHandshake(Ed25519PrivateKey.generate())
    for bad in (b"", b"XX1" + b"\x00" * 70, b"AH1" + b"\x00" * 64 + bytes([9]) + b"ab"):
        with pytest.raises(ch.ProtocolError):
            server.respond(bad)
    _, _, worker, _ = pair()
    with pytest.raises(ch.ProtocolError):
        worker.seal(b"x" * (ch.MAX_FRAME_PLAINTEXT + 1))


def test_enrolment_proof_is_bound_to_the_connection_and_the_secret():
    stored = ch.enrolment_public_hex("secret-token-value")
    t1 = pair()[1].transcript
    proof = ch.enrolment_proof("secret-token-value", t1)
    assert ch.verify_enrolment_proof(stored, proof, t1)
    assert not ch.verify_enrolment_proof(stored, proof, pair()[1].transcript)  # other connection
    assert not ch.verify_enrolment_proof(stored, ch.enrolment_proof("other", t1), t1)
    assert not ch.verify_enrolment_proof("zz", proof, t1)  # garbage never raises


def test_a_stored_enrolment_key_cannot_make_proofs():
    # What the hub stores is a public key: it verifies proofs but contains no secret.
    stored = ch.enrolment_public_hex("secret-token-value")
    assert "secret-token-value" not in stored and len(stored) == 64


def test_worker_signature_roundtrip_and_purpose_binding():
    worker_key = Ed25519PrivateKey.generate()
    pub = ch.public_key_bytes(worker_key)
    transcript = pair()[1].transcript
    sig = ch.worker_signature(worker_key, b"auth", transcript)
    assert ch.verify_worker_signature(pub, sig, b"auth", transcript)
    assert not ch.verify_worker_signature(pub, sig, b"enroll", transcript)
    assert not ch.verify_worker_signature(pub, sig, b"auth", b"\x00" * 32)
    assert not ch.verify_worker_signature(b"short", sig, b"auth", transcript)
