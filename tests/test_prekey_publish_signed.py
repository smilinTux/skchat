"""Task 4 - signed-prekey verification on the publish ROUTE (``POST /api/v1/prekey``).

The model-layer fail-closed intake (``pq_prekeys.store_app_prekey_bundle``) is
covered by ``test_prekey_intake_signed.py``. These tests exercise the HTTP route
``daemon_proxy.api_publish_prekey`` end to end via the FastAPI TestClient, proving
the wire contract Task 5 (app bundle-signing) must match:

* Flag OFF (default) - behaviour UNCHANGED: an unsigned bundle publishes (200).
* Flag ON (``SKCHAT_REQUIRE_SIGNED_PREKEYS``) - a missing/invalid signature is
  rejected with 400 and nothing is stored; a validly-signed bundle publishes
  (200) and appears in ``load_peer_bundles``.

Canonical signed bytes (the app MUST sign exactly these, Task 5):
``json.dumps({"hybrid_public_hex", "key_id", "suite"}, sort_keys=True,
separators=(",", ":")).encode("utf-8")`` - the recipe in
``skchat.prekey_sig._canonical_signed_bytes`` / ``verify_prekey_bundle``.

The published bundle carries the armored detached signature under ``sig`` (the
app field, per the plan); the route aliases it to the verifier's ``signature``
field so the existing ``prekey_sig`` helper verifies it without change. A bundle
that already carries ``signature`` is accepted too (back-compat).

PGP-only verification (no liboqs), so these run without a PQ backend.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient over the daemon_proxy router with an isolated prekey store.

    ``SKCHAT_HOME`` points at a fresh tmp dir so ``pq_prekeys`` reads/writes a
    clean peer store; the dataplane-auth flag is left unset (default off) so the
    route runs without CapAuth headers.
    """
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


@pytest.fixture()
def alice_crypto(alice_keys):
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


def _bundle() -> dict:
    pub_hex = "ab" * 32
    return {
        "owner": "chef",
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "key_id": pub_hex[:16],
        "device_id": "chef-web",
        "ratchet": "pqdr1",
    }


def _signed_with_sig(alice_crypto: ChatCrypto) -> dict:
    """A bundle signed by Alice, carrying the signature under the app's ``sig`` field."""
    signed = sign_prekey_bundle(alice_crypto, _bundle())
    signed["sig"] = signed.pop("signature")
    return signed


# --------------------------------------------------------------------------- #
# Flag OFF (default) - unchanged
# --------------------------------------------------------------------------- #


def test_flag_off_default_publishes_unsigned(client, monkeypatch):
    """Default (flag unset): an unsigned bundle publishes with 200."""
    monkeypatch.delenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", raising=False)
    r = client.post("/api/v1/prekey", json=_bundle())
    assert r.status_code == 200

    from skchat import pq_prekeys as PQ

    assert PQ.load_peer_bundle("chef") is not None


# --------------------------------------------------------------------------- #
# Flag ON - fail closed
# --------------------------------------------------------------------------- #


def test_flag_on_unsigned_rejected_400(client, monkeypatch, alice_keys):
    """Flag on + no signature -> 400, nothing stored."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: alice_pub)

    r = client.post("/api/v1/prekey", json=_bundle())
    assert r.status_code == 400

    from skchat import pq_prekeys as PQ

    assert PQ.load_peer_bundles("chef") == []


def test_flag_on_signed_sig_field_accepted_200(client, monkeypatch, alice_crypto, alice_keys):
    """Flag on + a validly-signed bundle (signature under ``sig``) -> 200 and stored."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: alice_pub)

    signed = _signed_with_sig(alice_crypto)
    r = client.post("/api/v1/prekey", json=signed)
    assert r.status_code == 200

    from skchat import pq_prekeys as PQ

    slots = PQ.load_peer_bundles("chef")
    assert len(slots) == 1
    assert slots[0]["hybrid_public_hex"] == signed["hybrid_public_hex"]


def test_flag_on_signature_field_also_accepted_200(
    client, monkeypatch, alice_crypto, alice_keys
):
    """Back-compat: a bundle carrying the verifier's native ``signature`` field -> 200."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: alice_pub)

    signed = sign_prekey_bundle(alice_crypto, _bundle())  # keeps "signature"
    r = client.post("/api/v1/prekey", json=signed)
    assert r.status_code == 200

    from skchat import pq_prekeys as PQ

    assert PQ.load_peer_bundle("chef") is not None


def test_flag_on_tampered_signature_rejected_400(client, monkeypatch, alice_crypto, alice_keys):
    """Flag on + prekey substitution after signing -> 400, nothing stored."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: alice_pub)

    signed = _signed_with_sig(alice_crypto)
    signed["hybrid_public_hex"] = "cd" * 32  # swap the key the signature covered

    r = client.post("/api/v1/prekey", json=signed)
    assert r.status_code == 400

    from skchat import pq_prekeys as PQ

    assert PQ.load_peer_bundles("chef") == []


def test_flag_on_wrong_identity_rejected_400(client, monkeypatch, alice_crypto, bob_keys):
    """Flag on + a valid signature verified against the WRONG identity key -> 400."""
    _, bob_pub = bob_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: bob_pub)

    signed = _signed_with_sig(alice_crypto)  # signed by Alice, verified vs Bob
    r = client.post("/api/v1/prekey", json=signed)
    assert r.status_code == 400

    from skchat import pq_prekeys as PQ

    assert PQ.load_peer_bundles("chef") == []
