"""Group key refresh: key EXISTING groups' members from the prekey store.

Groups created before their members published hybrid prekeys have members that
hold no group key (``_member_has_group_key`` is False) — a sealed send would skip
them (partial coverage). :func:`refresh_group_member_keys` /
:func:`refresh_all_group_keys` in ``daemon_proxy_groups`` complete distribution:
for each unkeyed member they look up the now-available hybrid prekey
(``pq_prekeys.collect_member_hybrid_keys``), stamp it on the membership row, seed
the epoch if the group turned keyable, and persist — idempotently.

Reuses the ``test_group_seal.py`` conventions: hybrid group, SKCHAT_HOME=tmp,
monkeypatched ``pq_prekeys.collect_member_hybrid_keys``.
"""

from __future__ import annotations

import pytest

from skchat import daemon_proxy_groups as G
from skchat import pq_prekeys as PQ
from skchat.group import GroupChat, MemberRole


@pytest.fixture
def groups_store(tmp_path, monkeypatch):
    """Persist groups under a tmp store (SKCHAT_HOME=tmp + patched _GROUPS_DIR)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    return tmp_path / "groups"


@pytest.fixture
def prekeys(monkeypatch):
    """A controllable prekey store: {uri: pubhex}. Only listed uris resolve."""
    store: dict[str, str] = {}

    def _collect(identities, *, self_agent=None):
        return {uri: store[uri] for uri in identities if uri in store}

    monkeypatch.setattr(PQ, "collect_member_hybrid_keys", _collect)
    return store


def _hybrid_group(members):
    """A hybrid group with *members* = [(uri, pubhex)]; no epoch seeded yet."""
    group = GroupChat(name="Ops", kem_suite="x25519-mlkem768")
    for i, (uri, pub) in enumerate(members):
        group.add_member(
            identity_uri=uri,
            role=MemberRole.ADMIN if i == 0 else MemberRole.MEMBER,
            hybrid_kem_public_hex=pub,
        )
    return group


def test_refresh_keys_unkeyed_member_and_seeds_epoch(groups_store, prekeys):
    """An unkeyed member whose prekey IS now available becomes keyed, the epoch is
    seeded, and the group is saved."""
    group = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),   # keyed
        ("capauth:bob@skworld.io", ""),            # unkeyed
    ])
    G.save_group(group)
    assert not group.epoch_secret_hex          # not seeded yet
    prekeys["capauth:bob@skworld.io"] = "bb" * 32

    res = G.refresh_group_member_keys(group)

    assert res["group_id"] == group.id
    assert res["keyed"] == ["capauth:bob@skworld.io"]
    assert res["still_unkeyed"] == []
    assert res["changed"] is True
    # Member now carries the prekey and the epoch secret was seeded.
    assert group.get_member("capauth:bob@skworld.io").hybrid_kem_public_hex == "bb" * 32
    assert group.epoch_secret_hex
    # And it was persisted.
    reloaded = G.load_group(group.id)
    assert reloaded.get_member("capauth:bob@skworld.io").hybrid_kem_public_hex == "bb" * 32
    assert reloaded.epoch_secret_hex == group.epoch_secret_hex


def test_refresh_leaves_member_without_prekey_unkeyed(groups_store, prekeys):
    """A member with NO published prekey stays in still_unkeyed; nothing crashes."""
    group = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),
        ("capauth:carol@skworld.io", ""),          # no prekey published
    ])
    G.save_group(group)

    res = G.refresh_group_member_keys(group)

    assert res["keyed"] == []
    assert res["still_unkeyed"] == ["capauth:carol@skworld.io"]
    assert res["changed"] is False
    assert not group.get_member("capauth:carol@skworld.io").hybrid_kem_public_hex


def test_refresh_already_keyed_group_is_idempotent(groups_store, prekeys):
    """An already-fully-keyed group reports changed=False; re-running keys nothing."""
    group = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),
        ("capauth:bob@skworld.io", "bb" * 32),
    ])
    group.ensure_epoch()
    G.save_group(group)

    res = G.refresh_group_member_keys(group)
    assert res["keyed"] == []
    assert res["still_unkeyed"] == []
    assert res["changed"] is False

    # And a second pass on a group we just keyed also changes nothing.
    group2 = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),
        ("capauth:bob@skworld.io", ""),
    ])
    G.save_group(group2)
    prekeys["capauth:bob@skworld.io"] = "bb" * 32
    assert G.refresh_group_member_keys(group2)["changed"] is True
    assert G.refresh_group_member_keys(group2)["changed"] is False


def test_refresh_all_group_keys_summary(groups_store, prekeys):
    """The sweep counts groups, changed groups, keyed slots, and still-partial groups."""
    # Group 1: bob unkeyed, prekey available -> becomes keyed (changed).
    g1 = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),
        ("capauth:bob@skworld.io", ""),
    ])
    # Group 2: carol unkeyed, NO prekey -> stays partial (unchanged).
    g2 = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),
        ("capauth:carol@skworld.io", ""),
    ])
    # Group 3: fully keyed already -> unchanged, not partial.
    g3 = _hybrid_group([
        ("capauth:alice@skworld.io", "aa" * 32),
        ("capauth:dave@skworld.io", "dd" * 32),
    ])
    g3.ensure_epoch()
    for g in (g1, g2, g3):
        G.save_group(g)
    prekeys["capauth:bob@skworld.io"] = "bb" * 32

    summary = G.refresh_all_group_keys()

    assert summary["groups"] == 3
    assert summary["groups_changed"] == 1          # only g1
    assert summary["member_slots_keyed"] == 1      # bob
    assert summary["groups_still_partial"] == 1    # only g2 (carol still unkeyed)
