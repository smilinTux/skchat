"""PQC Q2 — group epoch-ratchet tests (hybrid X25519+ML-KEM-768 distribution).

Covers the marquee Phase-1 item (``docs/quantum-resistance-architecture.md`` §3
S5, §5 Phase 1):

    * per-message key derivation (distinct per index; deterministic; reorder-safe)
    * epoch advance (distinct epoch secrets => distinct keys)
    * hybrid epoch-secret wrap/unwrap round-trip (the PQ leg)
    * forward secrecy: a removed member cannot derive the new epoch key
    * post-compromise security: a leaked epoch secret yields nothing about the next
    * re-key triggers: member add/remove + the 50-msg / 7-day bound
    * a 3-member hybrid group round-trips a message
    * loss / reorder tolerance
    * bandwidth: PQ (ML-KEM) material is paid once per epoch, not per message
    * back-compat: classical (rsa-pgp-wrap-v1) groups are untouched

These tests REQUIRE the liboqs-backed hybrid KEM (skcomms.pqkem); if it is not
importable they skip (a missing PQ backend is an environment gap, not a logic
failure — the classical suite is still exercised by test_group.py).
"""

from __future__ import annotations

import pytest

from skchat.group import GroupChat, GroupKeyDistributor, MemberRole

pqkem = pytest.importorskip("skcomms.pqkem")
gr = pytest.importorskip("skchat.group_ratchet")

if not pqkem.is_available():  # liboqs missing -> skip the whole module
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hybrid_keypair_hex() -> tuple[str, str]:
    kp = pqkem.hybrid_keypair()
    return kp.public_key.hex(), kp.private_key.hex()


def _make_hybrid_group(n_members: int = 3) -> tuple[GroupChat, dict[str, str]]:
    """Create a hybrid group with ``n_members`` each holding a hybrid key.

    Returns (group, {uri: hybrid_private_hex}).
    """
    g = GroupChat.create(name="PQ Squad", creator_uri="capauth:alice@skworld.io")
    privs: dict[str, str] = {}
    pubs: dict[str, str] = {}
    a_pub, a_priv = _hybrid_keypair_hex()
    privs["capauth:alice@skworld.io"] = a_priv
    pubs["capauth:alice@skworld.io"] = a_pub
    names = ["bob", "carol", "dave", "erin"]
    for i in range(1, n_members):
        uri = f"capauth:{names[i - 1]}@skworld.io"
        pub, priv = _hybrid_keypair_hex()
        g.add_member(identity_uri=uri, role=MemberRole.MEMBER)
        privs[uri] = priv
        pubs[uri] = pub
    g.migrate_to_hybrid(member_hybrid_keys=pubs)
    return g, privs


# ---------------------------------------------------------------------------
# 1. Per-message key derivation
# ---------------------------------------------------------------------------


def test_message_keys_distinct_per_index():
    secret = gr.new_epoch_secret()
    k0 = gr.derive_message_key(secret, epoch=1, index=0)
    k1 = gr.derive_message_key(secret, epoch=1, index=1)
    k2 = gr.derive_message_key(secret, epoch=1, index=2)
    assert len({k0, k1, k2}) == 3
    assert all(len(k) == 32 for k in (k0, k1, k2))


def test_message_key_deterministic():
    secret = gr.new_epoch_secret()
    a = gr.derive_message_key(secret, 5, 7)
    b = gr.derive_message_key(secret, 5, 7)
    assert a == b


def test_message_key_index_addressable_no_chain_state():
    """Any index derivable independently (loss/reorder tolerant)."""
    secret = gr.new_epoch_secret()
    # Deriving index 9 first, then 0, must match deriving them in order.
    k9_first = gr.derive_message_key(secret, 1, 9)
    k0 = gr.derive_message_key(secret, 1, 0)
    k9_again = gr.derive_message_key(secret, 1, 9)
    assert k9_first == k9_again
    assert k0 != k9_first


# ---------------------------------------------------------------------------
# 2. Distinct epochs => distinct keys
# ---------------------------------------------------------------------------


def test_distinct_epochs_distinct_keys_same_index():
    secret = gr.new_epoch_secret()
    e1 = gr.derive_message_key(secret, epoch=1, index=0)
    e2 = gr.derive_message_key(secret, epoch=2, index=0)
    assert e1 != e2


def test_distinct_epoch_secrets_distinct_keys():
    s1 = gr.new_epoch_secret()
    s2 = gr.new_epoch_secret()
    assert s1 != s2
    assert gr.derive_message_key(s1, 1, 0) != gr.derive_message_key(s2, 1, 0)


# ---------------------------------------------------------------------------
# 3. Hybrid wrap/unwrap round-trip (the PQ leg)
# ---------------------------------------------------------------------------


def test_epoch_secret_wrap_unwrap_roundtrip():
    pub_hex, priv_hex = _hybrid_keypair_hex()
    secret = gr.new_epoch_secret()
    payload = gr.wrap_epoch_secret(secret, bytes.fromhex(pub_hex))
    assert len(payload) == gr.WRAPPED_PAYLOAD_LEN
    recovered = gr.unwrap_epoch_secret(payload, bytes.fromhex(priv_hex))
    assert recovered == secret


def test_unwrap_with_wrong_key_fails():
    pub_hex, _ = _hybrid_keypair_hex()
    _, other_priv = _hybrid_keypair_hex()
    secret = gr.new_epoch_secret()
    payload = gr.wrap_epoch_secret(secret, bytes.fromhex(pub_hex))
    with pytest.raises(gr.GroupRatchetError):
        gr.unwrap_epoch_secret(payload, bytes.fromhex(other_priv))


# ---------------------------------------------------------------------------
# 4. EpochRatchet state + bounds
# ---------------------------------------------------------------------------


def test_ratchet_next_outbound_advances():
    r = gr.EpochRatchet(epoch=1, epoch_secret=gr.new_epoch_secret())
    i0, k0 = r.next_outbound_key()
    i1, k1 = r.next_outbound_key()
    assert (i0, i1) == (0, 1)
    assert k0 != k1
    assert r.message_index == 2


def test_ratchet_should_rekey_on_msg_bound():
    r = gr.EpochRatchet(epoch=1, epoch_secret=gr.new_epoch_secret(), rekey_msg_bound=3)
    assert not r.should_rekey()
    for _ in range(3):
        r.next_outbound_key()
    assert r.should_rekey()


def test_ratchet_should_rekey_on_age_bound():
    import time

    r = gr.EpochRatchet(
        epoch=1,
        epoch_secret=gr.new_epoch_secret(),
        rekey_age_seconds=0,  # immediately stale
        epoch_started_at=time.time() - 10,
    )
    assert r.should_rekey()


# ---------------------------------------------------------------------------
# 5. GroupChat hybrid integration — migration, round-trip, re-key triggers
# ---------------------------------------------------------------------------


def test_migrate_to_hybrid_sets_suite_and_epoch():
    g, _ = _make_hybrid_group(3)
    assert g.is_hybrid
    assert g.kem_suite == "x25519-mlkem768"
    assert g.epoch == 1
    assert g.epoch_secret_hex
    assert g.message_index == 0


def test_three_member_group_message_roundtrip():
    g, privs = _make_hybrid_group(3)
    env = g.encrypt_message("standup at 9?")
    assert env["suite"] == "x25519-mlkem768"
    assert env["epoch"] == 1
    assert env["index"] == 0
    # Each member recovers the epoch secret via hybrid decap, then decrypts.
    dist = GroupKeyDistributor.distribute_epoch_secret(g)
    for uri, priv_hex in privs.items():
        wrapped = dist[uri]
        assert wrapped is not None
        recovered = GroupKeyDistributor.unwrap_epoch_secret_for_member(wrapped, priv_hex)
        assert recovered == g.epoch_secret_hex
        # Receiver derives the same per-message key and decrypts.
        key = gr.derive_message_key(bytes.fromhex(recovered), env["epoch"], env["index"])
        from skchat.group import GroupMessageEncryptor

        assert GroupMessageEncryptor.decrypt(env["ciphertext"], key.hex()) == "standup at 9?"


def test_group_decrypt_message_self_roundtrip():
    g, _ = _make_hybrid_group(2)
    env = g.encrypt_message("hello pq world")
    assert g.decrypt_message(env) == "hello pq world"


def test_add_member_with_rekey_advances_epoch():
    g, _ = _make_hybrid_group(2)
    e0 = g.epoch
    s0 = g.epoch_secret_hex
    pub, _priv = _hybrid_keypair_hex()
    g.add_member(
        identity_uri="capauth:zoe@skworld.io",
        hybrid_kem_public_hex=pub,
        rekey=True,
    )
    assert g.epoch == e0 + 1
    assert g.epoch_secret_hex != s0


def test_remove_member_advances_epoch_and_bumps_version():
    g, _ = _make_hybrid_group(3)
    e0 = g.epoch
    v0 = g.key_version
    assert g.remove_member("capauth:bob@skworld.io") is True
    assert g.epoch == e0 + 1
    assert g.key_version == v0 + 1


def test_maybe_rekey_on_message_bound():
    g, _ = _make_hybrid_group(2)
    g.rekey_msg_bound = 3
    epochs_seen = {g.epoch}
    for _ in range(5):
        g.encrypt_message("msg")
        epochs_seen.add(g.epoch)
    # The bound (3) must have forced at least one epoch advance.
    assert max(epochs_seen) > 1


# ---------------------------------------------------------------------------
# 6. Forward secrecy (FS) + post-compromise security (PCS)
# ---------------------------------------------------------------------------


def test_forward_secrecy_removed_member_cannot_derive_new_epoch():
    g, privs = _make_hybrid_group(3)
    bob_uri = "capauth:bob@skworld.io"
    bob_priv = privs[bob_uri]
    # Capture pre-removal distribution to confirm Bob *could* read the old epoch.
    old_dist = GroupKeyDistributor.distribute_epoch_secret(g)
    assert old_dist[bob_uri] is not None
    # Remove Bob -> epoch advances, Bob is no longer in the member set.
    g.remove_member(bob_uri)
    new_dist = GroupKeyDistributor.distribute_epoch_secret(g)
    assert bob_uri not in new_dist  # not distributed to a removed member (FS)
    # Even if Bob replays his OLD wrapped payload, it decrypts the OLD secret,
    # which no longer matches the group's CURRENT epoch secret.
    old_secret = GroupKeyDistributor.unwrap_epoch_secret_for_member(
        old_dist[bob_uri], bob_priv
    )
    assert old_secret != g.epoch_secret_hex


def test_post_compromise_security_leaked_epoch_secret_useless_next_epoch():
    g, _ = _make_hybrid_group(2)
    leaked = g.epoch_secret_hex
    # An attacker who learns this epoch's secret can read this epoch...
    env_now = g.encrypt_message("current epoch msg")
    from skchat.group import GroupMessageEncryptor

    k = gr.derive_message_key(bytes.fromhex(leaked), env_now["epoch"], env_now["index"])
    assert GroupMessageEncryptor.decrypt(env_now["ciphertext"], k.hex()) == "current epoch msg"
    # ...but a re-key produces an INDEPENDENT secret the leak does not reveal.
    g.rotate_key(reason="manual_pcs")
    assert g.epoch_secret_hex != leaked
    env_next = g.encrypt_message("next epoch msg")
    bad_key = gr.derive_message_key(
        bytes.fromhex(leaked), env_next["epoch"], env_next["index"]
    )
    with pytest.raises(ValueError):
        GroupMessageEncryptor.decrypt(env_next["ciphertext"], bad_key.hex())


# ---------------------------------------------------------------------------
# 7. Loss / reorder tolerance
# ---------------------------------------------------------------------------


def test_loss_and_reorder_tolerance():
    g, _ = _make_hybrid_group(2)
    secret = g.epoch_secret_hex
    envs = [g.encrypt_message(f"m{i}") for i in range(5)]
    from skchat.group import GroupMessageEncryptor

    # Decrypt out of order, skipping some (loss): order 3, 0, 4 — gaps fine.
    for i in (3, 0, 4):
        env = envs[i]
        key = gr.derive_message_key(bytes.fromhex(secret), env["epoch"], env["index"])
        assert GroupMessageEncryptor.decrypt(env["ciphertext"], key.hex()) == f"m{i}"


# ---------------------------------------------------------------------------
# 8. Bandwidth — PQ material once per epoch, not per message
# ---------------------------------------------------------------------------


def test_pq_material_per_epoch_not_per_message():
    g, _ = _make_hybrid_group(2)
    # Per-epoch: each member's wrapped payload carries the ML-KEM ciphertext.
    dist = GroupKeyDistributor.distribute_epoch_secret(g)
    per_member_epoch_bytes = len(bytes.fromhex(next(p for p in dist.values() if p)))
    assert per_member_epoch_bytes == gr.WRAPPED_PAYLOAD_LEN  # ~1180 B incl. ML-KEM ct
    assert per_member_epoch_bytes > pqkem.CIPHERTEXT_LEN  # contains the 1120 B PQ ct

    # Per-message: the envelope carries NO KEM ciphertext — only AES-GCM output
    # plus tiny (epoch,index) ints. Confirm it is far smaller than the PQ leg.
    import base64

    env = g.encrypt_message("x" * 16)
    msg_ct_bytes = len(base64.b64decode(env["ciphertext"]))
    assert env.get("epoch") is not None and env.get("index") is not None
    # No 1.1 KB ML-KEM ciphertext rides per message.
    assert msg_ct_bytes < pqkem.CIPHERTEXT_LEN
    assert msg_ct_bytes < 100  # 12B nonce + 16B msg + 16B tag ~ 44 B


# ---------------------------------------------------------------------------
# 9. Graceful fallback — member without a hybrid key
# ---------------------------------------------------------------------------


def test_member_without_hybrid_key_skipped_gracefully():
    g = GroupChat.create(name="Mixed", creator_uri="capauth:alice@skworld.io")
    a_pub, _ = _hybrid_keypair_hex()
    g.add_member("capauth:nokey@skworld.io")  # no hybrid key
    g.migrate_to_hybrid(member_hybrid_keys={"capauth:alice@skworld.io": a_pub})
    dist = GroupKeyDistributor.distribute_epoch_secret(g)
    assert dist["capauth:alice@skworld.io"] is not None
    assert dist["capauth:nokey@skworld.io"] is None  # skipped, not crashed
    rpt = g.crypto_self_report()
    assert rpt["members_with_hybrid_key"] == 1
    assert rpt["members_total"] == 2


# ---------------------------------------------------------------------------
# 10. Self-report reflects reality per group
# ---------------------------------------------------------------------------


def test_self_report_hybrid_group_reports_hybrid_pq():
    g, _ = _make_hybrid_group(2)
    rpt = g.crypto_self_report()
    assert rpt["kem_suite"] == "x25519-mlkem768"
    assert rpt["status"] == "hybrid-pq"
    assert rpt["quantum_resistant"] is True
    assert "FIPS 203" in rpt["fips_refs"]


def test_self_report_classical_group_reports_classical():
    g = GroupChat.create(
        name="Legacy",
        creator_uri="capauth:alice@skworld.io",
        kem_suite="rsa-pgp-wrap-v1",
    )
    rpt = g.crypto_self_report()
    assert rpt["kem_suite"] == "rsa-pgp-wrap-v1"
    assert rpt["status"] == "classical"
    assert rpt["quantum_resistant"] is False


# ---------------------------------------------------------------------------
# 11. Back-compat — classical groups are completely untouched
# ---------------------------------------------------------------------------


def test_classical_group_unchanged_no_ratchet_fields_used():
    g = GroupChat.create(
        name="Classical",
        creator_uri="capauth:alice@skworld.io",
        kem_suite="rsa-pgp-wrap-v1",
    )
    assert not g.is_hybrid
    assert g.epoch == 0
    assert g.epoch_secret_hex == ""
    # Classical encrypt path uses the static group key, no epoch/index.
    env = g.encrypt_message("hi")
    assert env["epoch"] is None and env["index"] is None
    assert g.decrypt_message(env) == "hi"


def test_classical_rotate_key_behaviour_preserved():
    g = GroupChat.create(
        name="Classical",
        creator_uri="capauth:alice@skworld.io",
        kem_suite="rsa-pgp-wrap-v1",
    )
    old_key = g.group_key
    old_v = g.key_version
    g.rotate_key(reason="manual")
    assert g.group_key != old_key
    assert g.key_version == old_v + 1
    assert g.epoch == 0  # classical never advances the epoch
