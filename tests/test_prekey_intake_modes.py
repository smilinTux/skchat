"""End-to-end intake behaviour per mode, through the real FastAPI route.

The specific defect this guards: the intake used to gate signer resolution on a
BOOLEAN (`if PQ.require_signed_prekeys()`), which is false in shadow. Left as-is,
shadow would resolve no signer, log REJECT for every bundle including good ones,
and the soak would be worthless. The first test below is that regression.

Also pins the bypass invariant: publish_self_prekey writes via store_peer_bundle
and must stay UNGATED even in enforce, because lumina's and opus's live
self-published slots are unsigned and must keep working after the flip.
"""

from __future__ import annotations

import importlib
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"
ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def client(PQ, monkeypatch, alice_keys):
    """TestClient over the daemon router with the operator signer stubbed."""
    _, alice_pub = alice_keys
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: alice_pub)
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


@pytest.fixture()
def alice_crypto(alice_keys) -> ChatCrypto:
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


def _bundle() -> dict:
    pub_hex = "cd" * 32
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": "chef-web",
    }


def test_shadow_resolves_a_signer_and_accepts_a_good_bundle(
    client, PQ, monkeypatch, caplog, alice_crypto
):
    """THE regression: shadow must resolve the signer, not skip it."""
    signed = sign_prekey_bundle(alice_crypto, _bundle())
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        resp = client.post("/api/v1/prekey", json=signed)

    assert resp.status_code == 200
    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=ACCEPT" in line, (
        "shadow resolved no signer: the intake is still gating on a boolean"
    )
    assert "signer=daemon-attest" in line


def test_shadow_stores_an_unsigned_bundle_but_flags_it(client, PQ, monkeypatch, caplog):
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        resp = client.post("/api/v1/prekey", json=_bundle())

    assert resp.status_code == 200, "shadow never rejects"
    assert PQ.load_peer_bundle("chef") is not None
    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=REJECT" in line
    assert "reason=unsigned" in line


def test_enforce_rejects_an_unsigned_bundle(client, PQ, monkeypatch):
    monkeypatch.setenv(ENV, "1")

    resp = client.post("/api/v1/prekey", json=_bundle())

    assert resp.status_code == 400
    assert PQ.load_peer_bundle("chef") is None


def test_enforce_accepts_a_signed_bundle(client, PQ, monkeypatch, alice_crypto):
    signed = sign_prekey_bundle(alice_crypto, _bundle())
    monkeypatch.setenv(ENV, "1")

    resp = client.post("/api/v1/prekey", json=signed)

    assert resp.status_code == 200
    assert PQ.load_peer_bundle("chef") is not None


def test_off_is_unchanged(client, PQ, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)

    resp = client.post("/api/v1/prekey", json=_bundle())

    assert resp.status_code == 200
    assert PQ.load_peer_bundle("chef") is not None


def test_self_publish_bypasses_the_gate_even_in_enforce(PQ, monkeypatch):
    """Bypass invariant.

    lumina's and opus's LIVE slots are unsigned and are written by
    publish_self_prekey -> store_peer_bundle, which never traverses the gated
    app path. If this ever starts going through store_app_prekey_bundle, the
    flip would silently stop the resident agent from publishing its own prekey.
    """
    monkeypatch.setenv(ENV, "1")

    bundle = PQ.publish_self_prekey("lumina")

    assert bundle is not None
    assert PQ.load_peer_bundle("lumina") is not None
