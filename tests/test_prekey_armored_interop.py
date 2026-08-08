"""Interop proof: the app-path armored prekey signature is ACCEPTED by the server.

This is the definitive gate for flipping ``SKCHAT_REQUIRE_SIGNED_PREKEYS=1``.

Background
----------
The Flutter app cannot itself hold the operator's PGP *identity* key (that key
lives on the daemon, resolved by ``crypto.load_agent_crypto``). Its local
``PgpBridge`` only emits RAW RSA PKCS#1 base64, which pgpy's
``verify_prekey_bundle`` rejects. So the app delegates prekey-bundle signing to
the daemon: it POSTs the canonical identity fields to ``POST /api/v1/prekey/sign``
and the daemon returns an ASCII-ARMORED OpenPGP detached signature made with the
operator key.

These tests exercise the REAL route (``daemon_proxy.api_sign_prekey``) via the
FastAPI TestClient and prove the returned armored signature:

* is a real ``-----BEGIN PGP SIGNATURE-----`` block,
* is over EXACTLY the server's canonical bytes
  (``json.dumps({hybrid_public_hex, key_id, suite}, sort_keys=True,
  separators=(",", ":"))``),
* verifies True under ``prekey_sig.verify_prekey_bundle`` against the operator's
  PGP public key (the same key ``_load_peer_public_key`` loads), and
* is accepted end-to-end by ``POST /api/v1/prekey`` with the enforcement flag ON.

PGP-only (no liboqs), so this runs without a PQ backend.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.crypto import ChatCrypto
from skchat.prekey_sig import _canonical_signed_bytes, verify_prekey_bundle

PASSPHRASE = "test-passphrase-123"

SUITE = "x25519-mlkem768"
PUB_HEX = "07" * 1216  # a plausible 1216-byte hybrid public key, hex
KEY_ID = PUB_HEX[:16]

OPERATOR_TOKEN = "op-secret-token"
_OP_HEADERS = {"X-Operator-Token": OPERATOR_TOKEN}


@pytest.fixture()
def operator_crypto(alice_keys):
    """Stand-in for the daemon's operator ``ChatCrypto`` (``load_agent_crypto``)."""
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


@pytest.fixture()
def client(tmp_path, monkeypatch, operator_crypto):
    """TestClient over the daemon router with the operator signer stubbed in.

    ``SKCHAT_HOME`` isolates the prekey store; dataplane auth is left off; the
    operator gate is satisfied by presenting the configured operator token (the
    sign endpoint is operator-only, so the TestClient must authenticate).
    ``load_agent_crypto`` is patched to return a known key so the test controls
    both signer and verifier.
    """
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setattr("skchat.crypto.load_agent_crypto", lambda *a, **k: operator_crypto)
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def _sign_via_route(client) -> str:
    """Ask the daemon to sign the canonical fields; return the armored signature."""
    r = client.post(
        "/api/v1/prekey/sign",
        json={"hybrid_public_hex": PUB_HEX, "key_id": KEY_ID, "suite": SUITE},
        headers=_OP_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    return body["signature"]


def test_sign_route_returns_armored_block(client):
    """The daemon returns a real ASCII-armored OpenPGP detached signature."""
    sig = _sign_via_route(client)
    assert sig.startswith("-----BEGIN PGP SIGNATURE-----")
    assert "-----END PGP SIGNATURE-----" in sig


def test_armored_sig_verifies_under_operator_key(client, alice_keys):
    """THE interop proof: the app-path armored sig is accepted by verify_prekey_bundle."""
    _, operator_pub = alice_keys
    sig = _sign_via_route(client)

    bundle = {
        "suite": SUITE,
        "hybrid_public_hex": PUB_HEX,
        "key_id": KEY_ID,
        "signature": sig,
    }
    assert verify_prekey_bundle(bundle, operator_pub) is True


def test_sign_covers_exact_canonical_bytes(client, alice_keys):
    """The signature is over the server's canonical bytes, not some app variant.

    A bundle whose identity fields match the signed ones verifies; changing any
    signed field (prekey substitution) breaks it - proving the covered bytes are
    exactly ``{hybrid_public_hex, key_id, suite}`` canonicalized.
    """
    _, operator_pub = alice_keys
    sig = _sign_via_route(client)

    # Sanity: the canonical bytes are the compact, sorted form the app builds.
    expected = json.dumps(
        {"hybrid_public_hex": PUB_HEX, "key_id": KEY_ID, "suite": SUITE},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert (
        _canonical_signed_bytes({"hybrid_public_hex": PUB_HEX, "key_id": KEY_ID, "suite": SUITE})
        == expected
    )

    tampered = {
        "suite": SUITE,
        "hybrid_public_hex": "cd" * 1216,  # swapped prekey
        "key_id": KEY_ID,
        "signature": sig,
    }
    assert verify_prekey_bundle(tampered, operator_pub) is False


def test_wrong_identity_rejects(client, bob_keys):
    """The operator's armored sig does NOT verify under a different identity."""
    _, bob_pub = bob_keys
    sig = _sign_via_route(client)
    bundle = {
        "suite": SUITE,
        "hybrid_public_hex": PUB_HEX,
        "key_id": KEY_ID,
        "signature": sig,
    }
    assert verify_prekey_bundle(bundle, bob_pub) is False


def test_end_to_end_publish_accepts_daemon_signed_bundle(client, monkeypatch, alice_keys):
    """Full app path with enforcement ON: sign via the route, publish, gets 200.

    Mirrors the live flow under ``SKCHAT_REQUIRE_SIGNED_PREKEYS=1``: the app gets
    an armored signature from the daemon, attaches it under ``sig``, and publishes.
    The publish route verifies it against the operator key and stores it.
    """
    _, operator_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: operator_pub)

    sig = _sign_via_route(client)
    published = {
        "owner": "chef",
        "suite": SUITE,
        "hybrid_public_hex": PUB_HEX,
        "key_id": KEY_ID,
        "device_id": "chef-web",
        "sig": sig,  # the app field; the route aliases it to `signature`
    }
    r = client.post("/api/v1/prekey", json=published)
    assert r.status_code == 200, r.text

    from skchat import pq_prekeys as PQ

    slots = PQ.load_peer_bundles("chef")
    assert len(slots) == 1
    assert slots[0]["hybrid_public_hex"] == PUB_HEX


def test_sign_requires_fields(client):
    """A sign request missing hybrid_public_hex is a 400 (no blind signing oracle)."""
    r = client.post(
        "/api/v1/prekey/sign",
        json={"suite": SUITE, "key_id": KEY_ID},
        headers=_OP_HEADERS,
    )
    assert r.status_code == 400


def test_sign_requires_operator(client):
    """Without the operator token the sign oracle is closed (401) - no signature."""
    r = client.post(
        "/api/v1/prekey/sign",
        json={"hybrid_public_hex": PUB_HEX, "key_id": KEY_ID, "suite": SUITE},
    )
    assert r.status_code == 401
