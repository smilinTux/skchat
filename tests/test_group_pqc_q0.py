"""PQC Q0 — crypto-agility scaffolding tests for skchat GroupChat.

Covers:
    - GroupChat.kem_suite / epoch defaults.
    - Round-trip with the new fields.
    - Back-compat: a group serialized WITHOUT kem_suite/epoch still loads.
"""

from __future__ import annotations

from skchat.group import GroupChat


def _make_group() -> GroupChat:
    return GroupChat.create(name="Q0 Test", creator_uri="capauth:lumina@skworld.io")


def test_group_defaults_classical_kem_suite_and_epoch_zero():
    g = _make_group()
    assert g.kem_suite == "rsa-pgp-wrap-v1"
    assert g.epoch == 0


def test_group_kem_suite_round_trips():
    g = _make_group()
    g.kem_suite = "x25519-mlkem768-v2"
    g.epoch = 3
    loaded = GroupChat.model_validate_json(g.model_dump_json())
    assert loaded.kem_suite == "x25519-mlkem768-v2"
    assert loaded.epoch == 3


def test_backcompat_group_without_new_fields_loads():
    """A group serialized BEFORE kem_suite/epoch existed must still parse."""
    g = _make_group()
    data = g.model_dump(mode="json")
    data.pop("kem_suite", None)
    data.pop("epoch", None)
    loaded = GroupChat.model_validate(data)
    assert loaded.kem_suite == "rsa-pgp-wrap-v1"
    assert loaded.epoch == 0
    # Existing fields unaffected.
    assert loaded.key_version == g.key_version
    assert loaded.group_key == g.group_key
