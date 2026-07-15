"""Unit tests for the Mode-C accept/sign membership proof (``skchat.guest_accept``).

Covers the acceptance from ``docs/2026-07-15-sovereign-invite-join-architecture.md``
§4 (Mode C) + the review hardenings it folds in:
  * §4 step 3/4 — the peer builds & signs an ACCEPT ASSERTION; the operator
    counter-signs → a mutual, peer+operator-signed ``join_record`` that IS the
    membership proof (zero identity server). BOTH sigs must verify (fail-closed).
  * macaroon caveats — ``aud = peer_fp`` (only this peer may accept) + ``scope``
    are baked into the signed assertion and enforced at verify (wrong aud/scope →
    reject).
  * anti-downgrade — the assertion ``bc`` must echo the operator commitment.
  * H5 — ``ConsumedNonces`` is a local accept-list of burned invite ``jti``
    (bearer caps can't be un-shared) that ALSO carries pin revocations; a
    replayed ``jti`` or a revoked pin → reject.
  * canonical bytes are reproducible on both sides (reuse ``pq_invites`` canonical).
"""

from __future__ import annotations

import pytest

from skchat import guest_accept as GA
from skchat import pq_invites as PQI

# Matches tests/conftest.py's PASSPHRASE for the shared PGP key fixtures.
PASSPHRASE = "test-passphrase-123"


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def operator_crypto(alice_keys):
    """A ChatCrypto holding the shared Alice PGP identity key (the 'operator')."""
    from skchat.crypto import ChatCrypto

    priv_armor, _pub = alice_keys
    return ChatCrypto(priv_armor, PASSPHRASE)


@pytest.fixture
def peer_crypto(bob_keys):
    """A ChatCrypto holding the shared Bob PGP identity key (the accepting 'peer')."""
    from skchat.crypto import ChatCrypto

    priv_armor, _pub = bob_keys
    return ChatCrypto(priv_armor, PASSPHRASE)


@pytest.fixture
def scenario(operator_crypto, peer_crypto, alice_keys, bob_keys):
    """A fully built, valid accept-assertion + join-record + both signatures."""
    _apriv, op_pub = alice_keys
    _bpriv, peer_pub = bob_keys
    bc = PQI.bundle_commitment(op_pub, "deadbeef" * 8)

    assertion = GA.build_accept_assertion(
        invite_jti="jti-abc",
        accepter_pubkey=peer_pub,
        bc=bc,
        peer_kem_ct="KEM-CIPHERTEXT",
        ts=1234567890,
        scope="dm",
    )
    sig_peer = GA.sign_accept_assertion(peer_crypto, assertion)

    record = GA.build_join_record(
        invite_jti="jti-abc",
        operator_id="alice@op.realm",
        peer_id="bob@peer.realm",
        operator_bundle_fp=GA.pubkey_fingerprint(op_pub),
        peer_bundle_fp=GA.pubkey_fingerprint(peer_pub),
        accept_assertion=assertion,
        sig_peer=sig_peer,
        ts=1234567900,
    )
    sig_op = GA.sign_join_record(operator_crypto, record)

    return {
        "op_pub": op_pub,
        "peer_pub": peer_pub,
        "bc": bc,
        "assertion": assertion,
        "sig_peer": sig_peer,
        "record": record,
        "sig_op": sig_op,
    }


# ── Feature flag ─────────────────────────────────────────────────────────────
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(PQI.FLAG_ENV, raising=False)
    assert GA.pq_invites_enabled() is False


# ── Accept assertion (§4 step 3) ─────────────────────────────────────────────
def test_accept_assertion_roundtrips(scenario):
    assert (
        GA.verify_accept_assertion(
            scenario["assertion"],
            scenario["sig_peer"],
            scenario["peer_pub"],
            expected_bc=scenario["bc"],
        )
        is True
    )


def test_accept_assertion_forged_sig_rejected(scenario, operator_crypto):
    """A signature by any key other than the accepter's → reject (fail-closed)."""
    forged = GA.sign_accept_assertion(operator_crypto, scenario["assertion"])
    assert (
        GA.verify_accept_assertion(
            scenario["assertion"], forged, scenario["peer_pub"], expected_bc=scenario["bc"]
        )
        is False
    )
    # Missing signature → fail-closed.
    assert (
        GA.verify_accept_assertion(
            scenario["assertion"], "", scenario["peer_pub"], expected_bc=scenario["bc"]
        )
        is False
    )


def test_accept_assertion_bc_mismatch_rejected(scenario):
    """bc must echo the operator commitment; a mismatch aborts (anti-downgrade)."""
    assert (
        GA.verify_accept_assertion(
            scenario["assertion"], scenario["sig_peer"], scenario["peer_pub"], expected_bc="OTHER"
        )
        is False
    )


def test_accept_assertion_wrong_aud_rejected(peer_crypto, alice_keys, bob_keys):
    """aud caveat must equal the accepter's own fingerprint (wrong aud → reject)."""
    _apriv, op_pub = alice_keys
    _bpriv, peer_pub = bob_keys
    bc = PQI.bundle_commitment(op_pub, "spk")
    assertion = GA.build_accept_assertion(
        "jti-1", peer_pub, bc, "KEM", 111, scope="dm", aud="SOMEONE-ELSE-FP"
    )
    sig = GA.sign_accept_assertion(peer_crypto, assertion)
    assert GA.verify_accept_assertion(assertion, sig, peer_pub, expected_bc=bc) is False


def test_accept_assertion_bad_scope_rejected(peer_crypto, alice_keys, bob_keys):
    """scope caveat is enforced: only dm|group are acceptable."""
    _apriv, op_pub = alice_keys
    _bpriv, peer_pub = bob_keys
    bc = PQI.bundle_commitment(op_pub, "spk")
    assertion = GA.build_accept_assertion("jti-1", peer_pub, bc, "KEM", 111, scope="admin")
    sig = GA.sign_accept_assertion(peer_crypto, assertion)
    assert GA.verify_accept_assertion(assertion, sig, peer_pub, expected_bc=bc) is False
    # Mismatched expected_scope also rejects.
    ok = GA.build_accept_assertion("jti-1", peer_pub, bc, "KEM", 111, scope="dm")
    ok_sig = GA.sign_accept_assertion(peer_crypto, ok)
    assert (
        GA.verify_accept_assertion(ok, ok_sig, peer_pub, expected_bc=bc, expected_scope="group")
        is False
    )


# ── Join record (§4 step 4) — mutual peer+operator membership proof ──────────
def test_join_record_roundtrips(scenario):
    assert (
        GA.verify_join_record(
            scenario["record"],
            scenario["sig_op"],
            scenario["sig_peer"],
            scenario["op_pub"],
            scenario["peer_pub"],
            expected_bc=scenario["bc"],
        )
        is True
    )


def test_join_record_bad_operator_sig_rejected(scenario):
    assert (
        GA.verify_join_record(
            scenario["record"],
            "-----BEGIN PGP SIGNATURE-----\ngarbage\n-----END PGP SIGNATURE-----",
            scenario["sig_peer"],
            scenario["op_pub"],
            scenario["peer_pub"],
            expected_bc=scenario["bc"],
        )
        is False
    )
    # Missing operator sig → fail-closed.
    assert (
        GA.verify_join_record(
            scenario["record"],
            "",
            scenario["sig_peer"],
            scenario["op_pub"],
            scenario["peer_pub"],
            expected_bc=scenario["bc"],
        )
        is False
    )


def test_join_record_bad_peer_sig_rejected(scenario, operator_crypto):
    """A valid operator sig is not enough: the embedded peer sig must verify too."""
    forged_peer = GA.sign_accept_assertion(operator_crypto, scenario["assertion"])
    assert (
        GA.verify_join_record(
            scenario["record"],
            scenario["sig_op"],
            forged_peer,
            scenario["op_pub"],
            scenario["peer_pub"],
            expected_bc=scenario["bc"],
        )
        is False
    )


def test_join_record_wrong_peer_key_rejected(scenario, alice_keys):
    """The peer sig must verify under the peer's key, not the operator's."""
    _apriv, op_pub = alice_keys
    assert (
        GA.verify_join_record(
            scenario["record"],
            scenario["sig_op"],
            scenario["sig_peer"],
            scenario["op_pub"],
            op_pub,  # wrong: verifying the peer sig under the operator key
            expected_bc=scenario["bc"],
        )
        is False
    )


# ── consumed_nonces (H5) — burn accept-list + pin revocations ────────────────
def test_consumed_nonces_burn_is_single_use():
    nonces = GA.ConsumedNonces(":memory:")
    assert nonces.is_consumed("jti-x") is False
    assert nonces.mark_consumed("jti-x") is True  # first burn wins
    assert nonces.mark_consumed("jti-x") is False  # replay loses
    assert nonces.is_consumed("jti-x") is True


def test_consumed_nonces_pin_revocation():
    nonces = GA.ConsumedNonces(":memory:")
    assert nonces.is_pin_revoked("bob@peer.realm") is False
    nonces.revoke_pin("bob@peer.realm")
    assert nonces.is_pin_revoked("bob@peer.realm") is True


def test_join_record_replayed_jti_rejected(scenario):
    """First accept burns the invite jti; a replay of the same jti → reject."""
    nonces = GA.ConsumedNonces(":memory:")
    args = (
        scenario["record"],
        scenario["sig_op"],
        scenario["sig_peer"],
        scenario["op_pub"],
        scenario["peer_pub"],
    )
    assert GA.verify_join_record(*args, expected_bc=scenario["bc"], nonces=nonces) is True
    # Same jti already burned → replay rejected.
    assert GA.verify_join_record(*args, expected_bc=scenario["bc"], nonces=nonces) is False


def test_join_record_revoked_pin_rejected(scenario):
    """A revoked identity pin voids the membership proof (H5)."""
    nonces = GA.ConsumedNonces(":memory:")
    nonces.revoke_pin("bob@peer.realm")  # the peer's pin is revoked
    assert (
        GA.verify_join_record(
            scenario["record"],
            scenario["sig_op"],
            scenario["sig_peer"],
            scenario["op_pub"],
            scenario["peer_pub"],
            expected_bc=scenario["bc"],
            nonces=nonces,
        )
        is False
    )


# ── Canonical reproducibility (both sides) ───────────────────────────────────
def test_accept_assertion_canonical_reproducible():
    """The exact signed bytes are order-independent and reproducible both sides."""
    a = GA.build_accept_assertion("jti-1", "PEER", "BC", "KEM", 42, scope="dm")
    # Same logical content, keys inserted in a different order → identical bytes.
    b = dict(reversed(list(a.items())))
    assert PQI._canonical(a) == PQI._canonical(b)
