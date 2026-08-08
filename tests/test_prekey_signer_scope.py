"""Scoped operator attestation: who is allowed to be the signer for whom.

Before this change the daemon-agent key was an unscoped FALLBACK: it resolved
for ANY owner missing from the peer store, so a bundle published under any
made-up owner name verified against it. Measured on .158 on 2026-08-08:
chef, mallory, totally-made-up and lumina ALL resolved to the daemon key.

Two consequences, both fixed here:

  1. chef verified only because ~/.skcapstone/peers/chef.json happens not to
     exist. Creating that file would have silently started failing every app
     publish closed. The landmine test below pins that it no longer can.
  2. The signature attested "the operator daemon signed this", not "this owner
     owns this key".

The model now: owner == OPERATOR_ID resolves to the daemon attestation key and
ONLY that; every other owner must self-sign against its peer-store key.
"""

from __future__ import annotations

import pytest

from skchat import daemon_proxy


@pytest.fixture()
def daemon_key(monkeypatch, alice_keys):
    """Stub load_agent_crypto() so the daemon attestation key is alice's."""
    alice_priv, alice_pub = alice_keys

    class _FakeKey:
        def __init__(self, pub):
            self._pub = pub

        def __str__(self):
            return self._pub

    class _FakeCrypto:
        can_sign = True

        def __init__(self, pub):
            self._private_key = type("K", (), {"pubkey": _FakeKey(pub)})()

    from skchat import crypto as _crypto

    monkeypatch.setattr(_crypto, "load_agent_crypto", lambda: _FakeCrypto(alice_pub))
    return alice_pub


@pytest.fixture()
def peer_store(monkeypatch):
    """Control what the peer store returns, including 'file not found'."""
    from skchat import crypto as _crypto

    table: dict[str, str] = {}

    def _load(peer):
        if peer not in table:
            raise _crypto.CryptoError(f"Peer file not found: {peer}")
        return table[peer]

    monkeypatch.setattr(_crypto, "_load_peer_public_key", _load)
    return table


def test_operator_resolves_to_the_daemon_attestation_key(daemon_key, peer_store):
    assert daemon_proxy._resolve_signer_pubkey("chef") == daemon_key


def test_operator_still_resolves_to_daemon_key_when_chef_json_exists(
    daemon_key, peer_store, bob_keys
):
    """THE landmine test.

    Eight peers already carry a public_key in the live store. Before this change,
    the peer store won source-order, so the day someone created chef.json the
    operator path would have started failing closed with no warning.
    """
    _, bob_pub = bob_keys
    peer_store["chef"] = bob_pub

    assert daemon_proxy._resolve_signer_pubkey("chef") == daemon_key, (
        "operator attestation must not be overridable by a peer-store entry"
    )


def test_non_operator_with_a_peer_key_resolves_to_that_key(daemon_key, peer_store, bob_keys):
    _, bob_pub = bob_keys
    peer_store["opus"] = bob_pub

    assert daemon_proxy._resolve_signer_pubkey("opus") == bob_pub


@pytest.mark.parametrize("owner", ["mallory", "totally-made-up", "lumina"])
def test_unknown_owner_no_longer_falls_back_to_the_daemon_key(daemon_key, peer_store, owner):
    """The owner-spoofing surface: these all used to resolve to the daemon key."""
    assert daemon_proxy._resolve_signer_pubkey(owner) is None


def test_operator_resolves_none_when_the_daemon_cannot_sign(monkeypatch, peer_store):
    """Fail closed: no attestation key means no signer, not a silent pass."""
    from skchat import crypto as _crypto

    monkeypatch.setattr(_crypto, "load_agent_crypto", lambda: None)
    assert daemon_proxy._resolve_signer_pubkey("chef") is None


# --------------------------------------------------------------------------- #
# the source label used by the audit line
# --------------------------------------------------------------------------- #


def test_source_label_operator():
    assert daemon_proxy._signer_source_label("chef", "ARMOR") == "daemon-attest"


def test_source_label_peer():
    assert daemon_proxy._signer_source_label("opus", "ARMOR") == "peer-store"


@pytest.mark.parametrize("owner", ["chef", "opus"])
def test_source_label_none_when_unresolved(owner):
    assert daemon_proxy._signer_source_label(owner, None) == "none"


# --------------------------------------------------------------------------- #
# case- and prefix-insensitive operator match
#
# crypto._load_peer_public_key lowercases its input internally. Before the fix,
# _resolve_signer_pubkey compared the raw peer argument against
# _short_name(OPERATOR_ID) ("chef"), so an owner spelled "CHEF" skipped the
# operator branch, fell into the peer-store lookup, and resolved out of
# chef.json instead -- the exact "peer store can win for the operator" landmine
# this branch exists to close. Both _resolve_signer_pubkey and
# _signer_source_label must agree on every spelling below.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("owner", ["CHEF", "Chef", "chef@skworld.io", "capauth:chef@x"])
def test_operator_match_is_case_and_prefix_insensitive(daemon_key, peer_store, owner):
    assert daemon_proxy._resolve_signer_pubkey(owner) == daemon_key


@pytest.mark.parametrize("owner", ["CHEF", "Chef", "chef@skworld.io", "capauth:chef@x"])
def test_source_label_matches_resolver_for_every_operator_spelling(
    daemon_key, peer_store, owner
):
    signer = daemon_proxy._resolve_signer_pubkey(owner)
    assert daemon_proxy._signer_source_label(owner, signer) == "daemon-attest"
