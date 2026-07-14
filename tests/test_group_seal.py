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
