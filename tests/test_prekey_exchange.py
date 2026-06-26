"""Cross-node hybrid prekey exchange — pull a REMOTE agent's bundle over federation.

Foundation for the federated half of RFC-0001 P1: today co-resident agents
resolve each other through the shared local prekey store
(``pq_prekeys.publish_self_prekey``), but a peer on another node
(``jarvis@.41``) never lands in our store, so ``dm_manager`` can't ratchet to it.

``prekey_exchange.fetch_peer_prekey`` closes that gap: it GETs the remote
daemon's ``/api/v1/prekey/<peer>`` (the HTTP getter is INJECTED — no real network
call here), validates the response is a well-formed bundle, optionally verifies a
sovereign signature, and ``store_peer_bundle()``s it locally so
``peer_is_hybrid`` / the dm_manager resolver flip to True for a pqdr1 peer.

A bundle without the ``pqdr1`` capability is stored but stays classical
(downgrade-safe); a malformed/empty response stores nothing and returns None.
"""

from __future__ import annotations

import importlib

import pytest

pqkem = pytest.importorskip("skcomms.pqkem")

if not pqkem.is_available():
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    """Fresh pq_prekeys + prekey_exchange bound to an isolated SKCHAT_HOME."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys, prekey_exchange

    importlib.reload(pq_prekeys)
    importlib.reload(prekey_exchange)
    return pq_prekeys, prekey_exchange


def _resolver(_peer):
    # Stub federation addressing: a fake remote daemon base — no real network.
    return "https://remote-node.example/api/v1/s2s/inbox"


def test_pqdr1_bundle_is_stored_and_resolvable(mods):
    pq_prekeys, prekey_exchange = mods
    kp = pqkem.hybrid_keypair()
    remote_bundle = {
        "suite": pq_prekeys.HYBRID_SUITE,
        "hybrid_public_hex": kp.public_key.hex(),
        "key_id": kp.public_key.hex()[:16],
        "device_id": "jarvis-daemon",
        "ratchet": pq_prekeys.RATCHET_CAP,
        "signature": None,
    }

    seen = {}

    def http_get(url):
        seen["url"] = url
        return {"prekey": remote_bundle}

    out = prekey_exchange.fetch_peer_prekey(
        "jarvis@chef.skworld.io", http_get=http_get, inbox_resolver=_resolver
    )

    assert out is not None
    # GET hit the REMOTE daemon's prekey endpoint for the short name.
    assert seen["url"] == "https://remote-node.example/api/v1/prekey/jarvis"
    # Stored locally + resolvable as a hybrid, ratchet-capable peer.
    assert pq_prekeys.peer_is_hybrid("jarvis") is True
    stored = pq_prekeys.load_peer_bundle("jarvis")
    assert stored["hybrid_public_hex"] == kp.public_key.hex()
    assert stored["ratchet"] == pq_prekeys.RATCHET_CAP
    assert prekey_exchange.is_ratchet_capable(stored) is True


def test_classical_no_cap_bundle_stored_but_not_ratchet_capable(mods):
    pq_prekeys, prekey_exchange = mods
    kp = pqkem.hybrid_keypair()
    # Hybrid public key present, but NO pqdr1 capability advertised (app / older
    # client). Downgrade-safe: stored, but the dm_manager resolver won't ratchet.
    remote_bundle = {
        "suite": pq_prekeys.HYBRID_SUITE,
        "hybrid_public_hex": kp.public_key.hex(),
        "key_id": "app-1",
        # no "ratchet"
    }

    out = prekey_exchange.fetch_peer_prekey(
        "chef@chef.skworld.io",
        http_get=lambda url: remote_bundle,  # bare bundle (no envelope)
        inbox_resolver=_resolver,
    )

    assert out is not None
    assert pq_prekeys.load_peer_bundle("chef") is not None
    # No ratchet capability → not ratchet-capable even though it's hybrid.
    assert prekey_exchange.is_ratchet_capable(out) is False
    assert out.get("ratchet") is None


def test_malformed_response_returns_none_and_stores_nothing(mods):
    pq_prekeys, prekey_exchange = mods

    for bad in ({}, None, {"prekey": {}}, {"prekey": None}, "not-a-dict"):
        out = prekey_exchange.fetch_peer_prekey(
            "ghost@chef.skworld.io",
            http_get=lambda url, _b=bad: _b,
            inbox_resolver=_resolver,
        )
        assert out is None
        assert pq_prekeys.load_peer_bundle("ghost") is None


def test_unresolvable_peer_returns_none(mods):
    pq_prekeys, prekey_exchange = mods

    def http_get(url):  # should never be called
        raise AssertionError("http_get must not run when the peer is unroutable")

    out = prekey_exchange.fetch_peer_prekey(
        "nowhere@x.y", http_get=http_get, inbox_resolver=lambda _p: None
    )
    assert out is None
    assert pq_prekeys.load_peer_bundle("nowhere") is None


def test_bad_sovereign_signature_is_rejected(mods):
    pq_prekeys, prekey_exchange = mods
    kp = pqkem.hybrid_keypair()
    # Bundle claims a sovereign signature but it won't verify under the signer key.
    remote_bundle = {
        "suite": pq_prekeys.HYBRID_SUITE,
        "hybrid_public_hex": kp.public_key.hex(),
        "key_id": "jarvis-1",
        "ratchet": pq_prekeys.RATCHET_CAP,
        "signature": "-----BEGIN PGP SIGNATURE-----\nbogus\n-----END PGP SIGNATURE-----",
    }

    out = prekey_exchange.fetch_peer_prekey(
        "jarvis@chef.skworld.io",
        http_get=lambda url: {"prekey": remote_bundle},
        inbox_resolver=_resolver,
        signer_pubkey="-----BEGIN PGP PUBLIC KEY BLOCK-----\nx\n-----END PGP PUBLIC KEY BLOCK-----",
    )
    assert out is None
    assert pq_prekeys.load_peer_bundle("jarvis") is None
