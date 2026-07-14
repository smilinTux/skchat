"""SEAM 9 — group messages are SEALED before fan-out (flag-gated, fail-closed).

Group messages historically rode the fan-out path (``daemon_proxy_groups.fan_out_send``)
as CLEARTEXT while 1:1 DMs were sealed. This wires the group's own crypto
(``GroupChat.encrypt_message`` — classical static-key or hybrid epoch-ratchet, per
the group's suite) into the fan-out path, behind ``SKCHAT_SEAL_GROUPS`` (default
OFF so current delivery is unchanged). Fail closed: a member holding no group key
is SKIPPED, never fanned out cleartext.
"""

from __future__ import annotations

import pytest

from skchat import daemon_proxy_groups as G
from skchat.group import GroupChat, MemberRole
from skchat.history import ChatHistory


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """History + group store in a tmp dir; no real network/agent-inbox delivery."""
    # SKCHAT_HOME -> tmp so ChatHistory(store=None) builds its default SQLite store
    # under tmp (not the operator's LIVE ~/.skchat/memory) — flagged in review.
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    # Keep fan-out purely local to `hist`: no skcomms transport, no agent inbox.
    monkeypatch.setattr(G, "_delivery_transport", lambda identity: None)
    monkeypatch.setattr(G, "local_deliver_to_agent", lambda msg: False)
    return hist


def _classical_group():
    """A classical group with one KEYED member (has a group key) + one KEYLESS."""
    group = GroupChat(name="Ops", kem_suite="rsa-pgp-wrap-v1")
    group.add_member(identity_uri="capauth:alice@skworld.io", role=MemberRole.ADMIN,
                     public_key_armor="PGP-KEY")  # sender
    group.add_member(identity_uri="capauth:bob@skworld.io",
                     public_key_armor="PGP-KEY")  # keyed member
    group.add_member(identity_uri="capauth:carol@skworld.io",
                     public_key_armor="")  # keyless member
    return group


def _member_copy(hist, uri):
    rows = [m for m in hist.load(peer=uri, limit=50) if m.recipient == uri]
    return rows[0] if rows else None


def test_flag_off_default_leaves_delivery_cleartext(isolated, monkeypatch):
    """Default (flag unset): per-member copies stay cleartext — unchanged behaviour."""
    monkeypatch.delenv("SKCHAT_SEAL_GROUPS", raising=False)
    group = _classical_group()
    G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "hello team")

    bob = _member_copy(isolated, "capauth:bob@skworld.io")
    carol = _member_copy(isolated, "capauth:carol@skworld.io")
    assert bob is not None and bob.content == "hello team"
    assert carol is not None and carol.content == "hello team"


def test_flag_on_seals_before_fan_out(isolated, monkeypatch):
    """Flag on: a keyed member's fanned-out copy is CIPHERTEXT, not the plaintext."""
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _classical_group()
    G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "hello team")

    bob = _member_copy(isolated, "capauth:bob@skworld.io")
    assert bob is not None
    # Not cleartext on the wire.
    assert bob.content != "hello team"
    assert G.is_sealed_group_content(bob.content)
    # And it round-trips back to the plaintext for a keyed member.
    assert G.unseal_group_content(group, bob.content) == "hello team"


def test_flag_on_fails_closed_for_keyless_member(isolated, monkeypatch):
    """Flag on: a member with no group key is SKIPPED, never sent cleartext."""
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _classical_group()
    G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "secret")

    carol = _member_copy(isolated, "capauth:carol@skworld.io")
    # Fail closed: no fanned-out copy at all (and certainly not cleartext).
    assert carol is None


def test_flag_on_group_thread_copy_stays_readable(isolated, monkeypatch):
    """The operator's own group-thread copy (recipient=group:<id>) stays cleartext."""
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _classical_group()
    msg = G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "hi")

    assert msg.recipient == f"group:{group.id}"
    assert msg.content == "hi"


# ── SEAM 9 receive-side: unseal a sealed group body before it hits the canonical
# thread + the responder (wires the decrypt path PR #20 left unbuilt) ──────────

@pytest.fixture
def groups_dir(tmp_path, monkeypatch):
    """Group store in tmp; no ChatHistory / live ~/.skchat touched."""
    monkeypatch.delenv("SKCHAT_HOME", raising=False)
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    return tmp_path / "groups"


def _hybrid_group():
    group = GroupChat(name="Ops", kem_suite="x25519-mlkem768")
    group.add_member(identity_uri="capauth:alice@skworld.io", role=MemberRole.ADMIN,
                     public_key_armor="PGP-KEY")
    group.ensure_epoch()
    return group


def test_hybrid_seal_unseal_roundtrip():
    """Hybrid epoch-ratchet body seals and unseals back to the original (the
    coverage gap the review flagged: classical was tested, hybrid was not)."""
    group = _hybrid_group()
    assert group.is_hybrid
    sealed = G._seal_group_content(group, "hybrid secret body")
    assert G.is_sealed_group_content(sealed) and sealed != "hybrid secret body"
    assert G.unseal_group_content(group, sealed) == "hybrid secret body"


def test_classical_seal_unseal_roundtrip():
    group = _classical_group()
    sealed = G._seal_group_content(group, "classical body")
    assert G.is_sealed_group_content(sealed)
    assert G.unseal_group_content(group, sealed) == "classical body"


def test_unseal_incoming_group_message_decrypts_sealed(groups_dir):
    """A sealed incoming member copy is decrypted in place so the canonical thread
    and the responder both see cleartext (hybrid)."""
    from skchat.models import ChatMessage
    group = _hybrid_group()
    G.save_group(group)
    sealed = G._seal_group_content(group, "for the group")
    incoming = ChatMessage(sender="capauth:alice@skworld.io",
                           recipient="capauth:bob@skworld.io",   # member-copy shape
                           content=sealed, thread_id=group.id)
    out = G.unseal_incoming_group_message(incoming)
    assert out.content == "for the group"
    assert not G.is_sealed_group_content(out.content)


def test_unseal_incoming_passthrough_when_cleartext(groups_dir):
    """Cleartext (flag-off) bodies are a no-op — same object back, unchanged."""
    from skchat.models import ChatMessage
    msg = ChatMessage(sender="a", recipient="b", content="plain text", thread_id="gid")
    out = G.unseal_incoming_group_message(msg)
    assert out is msg and out.content == "plain text"


def test_unseal_incoming_failclosed_when_group_unavailable(groups_dir):
    """If the group/key can't be loaded, the body stays sealed and NOTHING crashes
    (unreadable-but-safe beats a poll-loop exception)."""
    from skchat.models import ChatMessage
    stray = _hybrid_group()                       # sealed, but never save_group()'d
    sealed = G._seal_group_content(stray, "unreachable")
    msg = ChatMessage(sender="a", recipient="b", content=sealed, thread_id="ghost-gid")
    out = G.unseal_incoming_group_message(msg)    # must not raise
    assert G.is_sealed_group_content(out.content)


# ── Observable seal-when-ready gate: seal only fully-ready groups; a not-ready
# group NEVER silently downgrades — it's cleartext-but-flagged (degraded) or, if
# encryption is required, refuses to send. Encryption state is always visible. ──

def _ready_hybrid_group():
    """A hybrid group that IS seal-ready: epoch secret set + every member keyed."""
    group = GroupChat(name="Secure", kem_suite="x25519-mlkem768")
    group.ensure_epoch()
    group.add_member(identity_uri="capauth:alice@skworld.io", role=MemberRole.ADMIN,
                     hybrid_kem_public_hex="aa" * 32)
    group.add_member(identity_uri="capauth:bob@skworld.io", hybrid_kem_public_hex="bb" * 32)
    return group


def test_seal_when_ready_group_is_sealed(isolated, monkeypatch):
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _ready_hybrid_group()
    st = G.group_encryption_status(group)
    assert st["state"] == "sealed" and st["all_members_keyed"] and st["sealing"]
    gmsg = G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "top secret")
    bob = _member_copy(isolated, "capauth:bob@skworld.io")
    assert bob is not None and G.is_sealed_group_content(bob.content)     # sealed on the wire
    assert gmsg.metadata["encryption_state"] == "sealed" and gmsg.metadata["sealed"] is True


def test_partial_when_some_unkeyed_seals_keyed_skips_keyless_loudly(isolated, monkeypatch, caplog):
    """Flag on + a member unkeyed: keyed members are SEALED (confidentiality kept),
    the unkeyed member is SKIPPED (never cleartext), and it's LOUD — state=partial,
    warning logged, keyed message flagged sealed=True + encryption_state=partial."""
    import logging
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _ready_hybrid_group()
    group.add_member(identity_uri="capauth:carol@skworld.io", hybrid_kem_public_hex="")  # UNKEYED
    st = G.group_encryption_status(group)
    assert st["state"] == "partial" and not st["all_members_keyed"]
    assert "capauth:carol@skworld.io" in st["unkeyed_members"]
    with caplog.at_level(logging.WARNING):
        gmsg = G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "secret body")
    bob = _member_copy(isolated, "capauth:bob@skworld.io")
    carol = _member_copy(isolated, "capauth:carol@skworld.io")
    assert bob is not None and G.is_sealed_group_content(bob.content)      # keyed: SEALED
    assert carol is None                                                   # keyless: SKIPPED, no cleartext
    assert gmsg.metadata["encryption_state"] == "partial" and gmsg.metadata["sealed"] is True
    assert gmsg.metadata.get("sealed_skipped") == ["capauth:carol@skworld.io"]
    assert any("PARTIAL" in r.message for r in caplog.records)             # loud, not silent


def test_encryption_required_group_refuses_when_not_ready(isolated, monkeypatch):
    """encryption_required NEVER downgrades: raise instead of sending cleartext."""
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _ready_hybrid_group()
    group.add_member(identity_uri="capauth:carol@skworld.io", hybrid_kem_public_hex="")  # unkeyed
    group.metadata["encryption_required"] = True
    assert G.group_encryption_status(group)["state"] == "blocked"
    with pytest.raises(G.GroupSealNotReadyError):
        G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "must not leak")
    assert _member_copy(isolated, "capauth:bob@skworld.io") is None       # nothing delivered


def test_required_group_seals_normally_when_ready(isolated, monkeypatch):
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    group = _ready_hybrid_group()
    group.metadata["encryption_required"] = True
    assert G.group_encryption_status(group)["state"] == "sealed"
    gmsg = G.fan_out_send(group, isolated, "capauth:alice@skworld.io", "sealed ok")
    assert gmsg.metadata["sealed"] is True


def test_flag_off_status_is_off(monkeypatch):
    monkeypatch.delenv("SKCHAT_SEAL_GROUPS", raising=False)
    assert G.group_encryption_status(_ready_hybrid_group())["state"] == "off"


def test_conversation_exposes_encryption_status(monkeypatch):
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    conv = G.group_to_conversation(_ready_hybrid_group())
    assert "encryption" in conv and conv["encryption"]["state"] == "sealed"


# ── receiver-side key distribution end-to-end (the missing half of SEAM 9) ─────

def test_group_key_distribution_end_to_end_hybrid(tmp_path, monkeypatch):
    """Full chain: sender keys a member + distributes the wrapped epoch secret;
    the receiver APPLIES the package and can then UNSEAL what the sender sealed.
    Proves the receive-half that was missing (sender distributed to no one who
    could apply it)."""
    from skchat import pq_prekeys as PQ
    if not PQ.available():
        pytest.skip("PQ KEM backend unavailable")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKAGENT", "receiver")
    from skchat.group import GroupChat, MemberRole, GroupKeyDistributor

    recv_uri = "capauth:receiver@skworld.io"
    pub, _priv = PQ.ensure_agent_keypair("receiver")           # receiver's real keypair

    # SENDER: hybrid group; receiver is a keyed member; seed the epoch secret.
    sender = GroupChat(name="Secure", kem_suite="x25519-mlkem768")
    sender.add_member(identity_uri="capauth:sender@skworld.io", role=MemberRole.ADMIN)
    sender.add_member(identity_uri=recv_uri, hybrid_kem_public_hex=pub.hex())
    sender.ensure_epoch()
    assert sender.epoch_secret_hex

    dists = GroupKeyDistributor.distribute_key(sender)          # wrap to each member
    assert dists.get(recv_uri)
    package = {"type": "group_epoch_advance", "group_id": sender.id,
               "epoch": sender.epoch, "key_version": sender.key_version,
               "kem_suite": sender.kem_suite, "distributions": dists}

    # RECEIVER: a local group copy, same id, NO epoch secret yet.
    recv = GroupChat(name="Secure", kem_suite="x25519-mlkem768")
    recv.id = sender.id
    recv.add_member(identity_uri=recv_uri, role=MemberRole.ADMIN)
    assert not recv.epoch_secret_hex
    G.save_group(recv)

    # APPLY the package -> receiver gets the SAME epoch secret.
    assert G.apply_group_key_package(package, self_uri=recv_uri, agent="receiver") is True
    recv2 = G.load_group(sender.id)
    assert recv2.epoch_secret_hex == sender.epoch_secret_hex
    assert recv2.epoch == sender.epoch

    # END TO END: sender seals, receiver unseals.
    sealed = G._seal_group_content(sender, "cross-daemon secret")
    assert G.unseal_group_content(recv2, sealed) == "cross-daemon secret"


def test_apply_group_key_package_ignores_not_for_us(tmp_path, monkeypatch):
    """A package with no distribution for us (or wrong type) is a safe no-op."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    assert G.apply_group_key_package({"type": "group_key_rotation"}, self_uri="x") is False
    assert G.apply_group_key_package(
        {"type": "group_epoch_advance", "group_id": "g", "distributions": {}},
        self_uri="capauth:me@skworld.io") is False
