"""Tests for the 1:1 DM epoch-ratchet (RFC-0001 P1 — Level-3 periodic PQ rekey).

The DM ratchet mirrors :mod:`skchat.group_ratchet` for pairwise conversations:
a per-conversation epoch secret distributed via the hybrid KEM once per epoch,
per-message keys derived symmetrically, periodic rekey (msg-count / age) giving
forward secrecy + post-compromise security — i.e. lifting 1:1 from the stateless
one-shot seal (Level 2) to a running ratchet (Level 3).
"""

from __future__ import annotations

from skchat.dm_ratchet import derive_dm_message_key


def test_dm_message_key_is_deterministic_and_index_distinct():
    secret = b"\x01" * 32
    k0a = derive_dm_message_key(secret, epoch=0, index=0)
    k0b = derive_dm_message_key(secret, epoch=0, index=0)
    k1 = derive_dm_message_key(secret, epoch=0, index=1)

    assert len(k0a) == 32  # AES-256 message key
    assert k0a == k0b  # deterministic / index-addressable (loss+reorder tolerant)
    assert k0a != k1  # distinct per index


def test_dm_message_key_is_domain_separated_from_group_keys():
    """A DM key MUST never equal a group key for the same (secret, epoch, index)."""
    from skchat.group_ratchet import derive_message_key as derive_group_key

    secret = b"\x02" * 32
    dm = derive_dm_message_key(secret, epoch=3, index=7)
    group = derive_group_key(secret, epoch=3, index=7)

    assert dm != group  # distinct HKDF domain-separation labels


# --- DmRatchet state: outbound counter + periodic-rekey policy ---------------


def test_dm_ratchet_next_outbound_advances_index_with_distinct_keys():
    from skchat.dm_ratchet import DmRatchet

    r = DmRatchet(epoch=0, epoch_secret=b"\x03" * 32)
    i0, k0 = r.next_outbound_key()
    i1, k1 = r.next_outbound_key()

    assert (i0, i1) == (0, 1)  # monotone counter
    assert r.message_index == 2  # advanced
    assert k0 != k1
    assert k0 == derive_dm_message_key(b"\x03" * 32, 0, 0)  # matches derivation


def test_dm_ratchet_should_rekey_on_message_bound():
    from skchat.dm_ratchet import DmRatchet

    r = DmRatchet(epoch=0, epoch_secret=b"\x04" * 32, rekey_msg_bound=3)
    assert r.should_rekey() is False
    r.message_index = 3
    assert r.should_rekey() is True  # 50/3 msgs → rekey (PQ heal)


def test_dm_ratchet_should_rekey_on_age():
    from skchat.dm_ratchet import DmRatchet

    r = DmRatchet(
        epoch=0,
        epoch_secret=b"\x05" * 32,
        rekey_age_seconds=100,
        epoch_started_at=1_000.0,
    )
    assert r.should_rekey(now=1_050.0) is False
    assert r.should_rekey(now=1_100.0) is True  # 7-day bound → rekey


# --- Epoch-secret distribution via the hybrid KEM (once per epoch) -----------


def test_wrap_unwrap_epoch_secret_roundtrip():
    from skcomms.pqkem import hybrid_keypair

    from skchat.dm_ratchet import new_epoch_secret, unwrap_dm_epoch_secret, wrap_dm_epoch_secret

    kp = hybrid_keypair()
    secret = new_epoch_secret()
    payload = wrap_dm_epoch_secret(secret, kp.public_key)
    recovered = unwrap_dm_epoch_secret(payload, kp.private_key)

    assert recovered == secret
    assert len(secret) == 32


def test_new_epoch_secret_gives_independent_secrets():
    """Post-compromise security: a fresh epoch secret is unrelated to the last."""
    from skchat.dm_ratchet import new_epoch_secret

    assert new_epoch_secret() != new_epoch_secret()


def test_two_party_roundtrip_with_post_compromise_rekey():
    """Alice→Bob full path: wrap epoch secret over hybrid KEM, both derive the same
    per-message key, AES-256-GCM seals/opens; after rekey, the old key is useless."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from skcomms.pqkem import hybrid_keypair

    from skchat.dm_ratchet import (
        DmRatchet,
        new_epoch_secret,
        unwrap_dm_epoch_secret,
        wrap_dm_epoch_secret,
    )

    bob = hybrid_keypair()

    # Epoch 0 — Alice distributes the epoch secret to Bob, both build a ratchet.
    e0 = new_epoch_secret()
    bob_e0 = unwrap_dm_epoch_secret(wrap_dm_epoch_secret(e0, bob.public_key), bob.private_key)
    alice_r = DmRatchet(epoch=0, epoch_secret=e0)
    bob_r = DmRatchet(epoch=0, epoch_secret=bob_e0)

    idx, send_key = alice_r.next_outbound_key()
    nonce = os.urandom(12)
    ct = AESGCM(send_key).encrypt(nonce, b"hello bob", None)
    recv_key = bob_r.message_key(index=idx)
    assert AESGCM(recv_key).decrypt(nonce, ct, None) == b"hello bob"

    # Epoch 1 — periodic rekey. The old per-message key cannot open the new epoch.
    e1 = new_epoch_secret()
    assert e1 != e0
    new_key = DmRatchet(epoch=1, epoch_secret=e1).message_key(index=0)
    assert new_key != recv_key  # PCS: rekey heals — prior key is dead
