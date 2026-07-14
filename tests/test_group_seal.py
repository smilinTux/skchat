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
    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.delenv("SKCHAT_HOME", raising=False)
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
