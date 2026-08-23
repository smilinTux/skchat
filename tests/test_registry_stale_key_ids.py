"""Re-enrolling must not carry forward key_ids whose slot files are gone.

Observed on the live node. The operator unlinked a device and re-linked it.
Unlink DELETED that device's prekey slots, then ``record_enroll`` preserved the
now-dangling ``key_ids`` anyway, so the row claimed a slot that did not exist.

Preserving ids across a re-enroll is right in general (a device can re-enroll
its same key without ever having been unlinked, and its slots are still there),
but it is wrong for exactly the unlink-then-relink flow, which is the common one:
unlink is what deletes the slots, so every id it preserved is guaranteed stale.

Two things break when a stale id survives:

  * ``registry_had_no_slots`` stays False, so the loud "this device's prekey
    slots cannot be located and may survive unlink" warning is suppressed for a
    device that genuinely has no locatable slot. That warning exists precisely
    to stop a silent partial unlink.
  * the list reports the device as having published a prekey when it has not, so
    an operator cannot see that fanout currently has nowhere to send.

So prune to the ids whose slot file is actually on disk.
"""

from __future__ import annotations

import pytest

from skchat import device_registry as DR
from skchat import pq_prekeys as PQ


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (read nowhere).
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    yield


def _enroll(fp: str, label: str = "L") -> None:
    DR.record_enroll(fp, label=label, label_source="derived", platform="web", user_agent="UA")


def _store_slot(key_id: str) -> None:
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": key_id + "00" * 8, "key_id": key_id},
    )


def test_a_reenroll_drops_key_ids_whose_slot_was_deleted():
    """The live case: unlink removed the slot, so the id must not survive."""
    fp = "aa" * 8
    _enroll(fp)
    _store_slot("1111111111111111")
    DR.record_publish(fp, "1111111111111111")
    assert DR.get_device(fp)["key_ids"] == ["1111111111111111"]

    # Unlink deletes the slot file.
    assert PQ.remove_peer_bundle("chef", "1111111111111111") is True

    _enroll(fp)  # the operator re-links the same device

    assert DR.get_device(fp)["key_ids"] == [], "a dangling id must not be carried forward"


def test_a_reenroll_keeps_key_ids_whose_slot_still_exists():
    """Not a blanket wipe: a re-enroll with the slots intact must preserve them.

    This is the correlation-integrity property the preservation existed for, and
    it still has to hold, otherwise a later unlink cannot find the live slot.
    """
    fp = "bb" * 8
    _enroll(fp)
    _store_slot("2222222222222222")
    DR.record_publish(fp, "2222222222222222")

    _enroll(fp)

    assert DR.get_device(fp)["key_ids"] == ["2222222222222222"]


def test_a_reenroll_keeps_only_the_live_ids_when_some_are_stale():
    fp = "cc" * 8
    _enroll(fp)
    for k in ("3333333333333333", "4444444444444444"):
        _store_slot(k)
        DR.record_publish(fp, k)
    PQ.remove_peer_bundle("chef", "3333333333333333")

    _enroll(fp)

    assert DR.get_device(fp)["key_ids"] == ["4444444444444444"]


def test_pruning_makes_the_no_slots_warning_fire_again():
    """The reason this matters: the unlink warning must not stay suppressed.

    With a stale id preserved, ``registry_had_no_slots`` reads False and unlink
    reports a clean result while removing nothing.
    """
    from capauth.pairing import DeviceStore

    from skchat import device_unlink as DU

    fp = "dd" * 8
    _enroll(fp)
    _store_slot("5555555555555555")
    DR.record_publish(fp, "5555555555555555")
    PQ.remove_peer_bundle("chef", "5555555555555555")
    _enroll(fp)

    store = DeviceStore(DR.registry_path().parent / "devices.json")
    store._data[fp] = "PUBKEY"  # present in the store without a real handshake
    report = DU.unlink_device(fp, device_store=store)

    assert report["registry_had_no_slots"] is True
    assert report["slots_removed"] == []
    assert report["slots_failed"] == []


def test_pruning_survives_an_unreadable_prekey_dir():
    """Never let a prekey-store problem break enrollment: degrade to keeping."""
    fp = "ee" * 8
    _enroll(fp)
    _store_slot("6666666666666666")
    DR.record_publish(fp, "6666666666666666")

    import skchat.device_registry as mod

    def _boom(*_a, **_k):
        raise OSError("prekey store unavailable")

    original = mod._live_slot_ids
    mod._live_slot_ids = _boom
    try:
        _enroll(fp)
    finally:
        mod._live_slot_ids = original

    assert DR.get_device(fp)["key_ids"] == ["6666666666666666"]
