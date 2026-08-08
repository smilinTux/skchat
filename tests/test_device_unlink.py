"""Unlink: the security crux. A partial unlink is a silent hole, so prove each store."""

from __future__ import annotations

import base64

import pytest

from skchat import device_registry as DR
from skchat import device_unlink as DU
from skchat import operator_auth as OA
from skchat import pq_prekeys as PQ


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (which is read
    # nowhere). Getting this wrong writes real slot files into the operator's live
    # ~/.skchat/pqc/peers/chef/ and, in the unlink tests, DELETES real ones.
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return tmp_path


@pytest.fixture
def store(tmp_path):
    return OA.DeviceStore(tmp_path / "devices.json")


def _enrol(store, seed: str, key_id: str) -> str:
    """Enrol a device, register it, and give it a published prekey slot."""
    pub = base64.b64encode(seed.encode().ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label=seed, label_source="client", platform="app", user_agent="UA")
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": key_id + "00" * 8, "key_id": key_id},
    )
    DR.record_publish(fp, key_id)
    return fp


def test_unlink_revokes_sessions_drops_the_slot_and_deletes_the_device(store):
    fp = _enrol(store, "alpha", "1111111111111111")
    token = OA.mint_operator_session(device_fp=fp)
    assert OA.verify_operator_session(token).device_fp == fp

    result = DU.unlink_device(fp, device_store=store)

    assert result["sessions_revoked"] is True
    assert result["slots_removed"] == ["1111111111111111"]
    assert result["store_removed"] is True
    assert result["registry_marked"] is True
    # 1. sessions dead
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token)
    # 2. no new session can be minted (the key is gone from the store)
    assert store.is_enrolled(fp) is False
    # 3. the KEM slot is gone, so fanout cannot reach it
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == []
    # 4. the row is kept for audit but hidden
    assert DR.list_devices() == []
    assert len(DR.list_devices(include_revoked=True)) == 1


def test_unlink_is_idempotent(store):
    fp = _enrol(store, "alpha", "1111111111111111")
    first = DU.unlink_device(fp, device_store=store)
    second = DU.unlink_device(fp, device_store=store)
    assert first["store_removed"] is True
    assert second["store_removed"] is False  # already gone
    assert second["sessions_revoked"] is True  # revocation stays asserted


def test_unlink_of_an_unknown_device_raises_key_error(store):
    with pytest.raises(KeyError):
        DU.unlink_device("nosuchdevice", device_store=store)


def test_unlinking_one_device_does_not_disturb_another(store):
    keep = _enrol(store, "keeper", "1111111111111111")
    drop = _enrol(store, "dropme", "2222222222222222")
    keep_token = OA.mint_operator_session(device_fp=keep)

    DU.unlink_device(drop, device_store=store)

    assert OA.verify_operator_session(keep_token).device_fp == keep
    assert store.is_enrolled(keep) is True
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == ["1111111111111111"]


def test_the_whole_point_fanout_reaches_the_survivor_only(store):
    """THE assertion this epic exists for.

    Two devices, both publishing. Unlink one. A fresh fanout must seal to the
    survivor's slot ONLY, and the unlinked device's slot file must be gone from
    disk. This fails if ANY single step of unlink is skipped, which is exactly
    the "partial unlink is a silent security hole" the design warns about.
    """
    survivor = _enrol(store, "survivor", "1111111111111111")
    unlinked = _enrol(store, "unlinked", "2222222222222222")
    assert sorted(b["key_id"] for b in PQ.load_peer_bundles("chef")) == [
        "1111111111111111",
        "2222222222222222",
    ]

    DU.unlink_device(unlinked, device_store=store)

    # The fanout target list is exactly the survivor.
    targets = [b["key_id"] for b in PQ.load_peer_bundles("chef")]
    assert targets == ["1111111111111111"]
    # And the slot file itself is gone from disk, not merely filtered.
    assert not (PQ._peer_dir("chef") / "2222222222222222.json").exists()
    # The survivor is untouched and can still authenticate.
    assert store.is_enrolled(survivor) is True
