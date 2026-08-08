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
    # This suite is about unlink behavior for already-linked devices, not the
    # Phase 3 approval gate, so approve it the way an operator would.
    DR.set_approved(fp, True)
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


# --------------------------------------------------------------------------- #
# Failure-path tests (spec review findings): a partial unlink must be LOUD,
# never a success-shaped dict while the device can still receive messages it
# can decrypt.
# --------------------------------------------------------------------------- #


def test_a_raising_slot_removal_is_reported_failed_not_silently_clean(store, monkeypatch):
    """Critical 2: a swallowed step-2 failure must not report as a clean unlink."""
    fp = _enrol(store, "alpha", "1111111111111111")

    def _boom(owner, key_id):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(PQ, "remove_peer_bundle", _boom)

    result = DU.unlink_device(fp, device_store=store)

    assert result["slots_removed"] == []
    assert result["slots_failed"] == ["1111111111111111"]
    # The slot is still really on disk: the failure was not swept under the rug.
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == ["1111111111111111"]
    # The rest of the unlink still proceeded (device is still more locked out).
    assert result["store_removed"] is True
    assert store.is_enrolled(fp) is False


def test_no_registry_row_sets_registry_had_no_slots_and_warns(store, caplog):
    """Critical 1 (row is None case): a device enrolled only in the DeviceStore,
    never registered, cannot have its prekey slots located from the registry."""
    pub = base64.b64encode(b"ghost".ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    assert DR.get_device(fp) is None

    with caplog.at_level("WARNING"):
        result = DU.unlink_device(fp, device_store=store)

    assert result["registry_had_no_slots"] is True
    assert result["slots_removed"] == []
    assert result["slots_failed"] == []
    assert any(fp in rec.message for rec in caplog.records)


def test_empty_key_ids_sets_registry_had_no_slots_and_warns(store, caplog):
    """Critical 1 (empty key_ids case): a device that published before its
    registry row existed leaves key_ids empty, so its slot cannot be located."""
    pub = base64.b64encode(b"early".ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label="early", label_source="client", platform="app", user_agent="UA")
    assert DR.get_device(fp)["key_ids"] == []

    with caplog.at_level("WARNING"):
        result = DU.unlink_device(fp, device_store=store)

    assert result["registry_had_no_slots"] is True
    assert any(fp in rec.message for rec in caplog.records)


def test_publish_landing_mid_unlink_is_swept(store, monkeypatch):
    """Critical 3: a publish racing the unlink (in-flight app-path request that
    started before step 1 closed it) must still be swept, not left live."""
    fp = _enrol(store, "racer", "1111111111111111")

    original_remove = PQ.remove_peer_bundle
    injected = {"done": False}

    def racing_remove(owner, key_id):
        if not injected["done"]:
            injected["done"] = True
            # A publish landing between the initial snapshot and its removal.
            PQ.store_peer_bundle(
                "chef",
                {
                    "suite": "x25519-mlkem768",
                    "hybrid_public_hex": "2222222222222222" + "00" * 8,
                    "key_id": "2222222222222222",
                },
            )
            DR.record_publish(fp, "2222222222222222")
        return original_remove(owner, key_id)

    monkeypatch.setattr(PQ, "remove_peer_bundle", racing_remove)

    result = DU.unlink_device(fp, device_store=store)

    assert sorted(result["slots_removed"]) == ["1111111111111111", "2222222222222222"]
    assert PQ.load_peer_bundles("chef") == []


def test_persistent_racing_publisher_leaves_visible_unswept_slots(store, monkeypatch):
    """Small 1 (residual edge of Critical 3): the sweep is capped at
    _MAX_SLOT_SWEEP_PASSES so it cannot spin forever against a publisher that
    keeps racing ahead of it. Whatever the cap leaves unswept must still be
    visible in the report, not a success-shaped dict hiding a live slot."""
    fp = _enrol(store, "persistent", "0000000000000000")

    original_remove = PQ.remove_peer_bundle
    counter = {"n": 0}

    def racing_remove(owner, key_id):
        counter["n"] += 1
        # Every single removal triggers ANOTHER publish, so the sweep never
        # reaches a stable (nothing-new) pass within the cap.
        new_key_id = f"{counter['n']:016d}"
        PQ.store_peer_bundle(
            "chef",
            {
                "suite": "x25519-mlkem768",
                "hybrid_public_hex": new_key_id + "00" * 8,
                "key_id": new_key_id,
            },
        )
        DR.record_publish(fp, new_key_id)
        return original_remove(owner, key_id)

    monkeypatch.setattr(PQ, "remove_peer_bundle", racing_remove)

    result = DU.unlink_device(fp, device_store=store)

    remaining_on_disk = [b["key_id"] for b in PQ.load_peer_bundles("chef")]
    assert remaining_on_disk, "expected the cap to leave a live slot behind"
    # The still-live slot must be visibly reported, not silently dropped.
    assert set(remaining_on_disk) <= set(result["slots_failed"])


def test_idempotent_reunlink_does_not_false_positive_slots_failed(store):
    """Small 2: remove_peer_bundle returns False both for "already absent"
    and for a swallowed OSError, and the registry row keeps its key_ids after
    unlink. A second, idempotent unlink of an already-unlinked device must
    not report a false slots_failed for a slot that is genuinely gone."""
    fp = _enrol(store, "alpha", "1111111111111111")

    first = DU.unlink_device(fp, device_store=store)
    assert first["slots_removed"] == ["1111111111111111"]
    assert first["slots_failed"] == []

    second = DU.unlink_device(fp, device_store=store)
    assert second["slots_failed"] == []


def test_mark_revoked_raising_does_not_propagate(store, monkeypatch):
    """Important 4: step 5 must never raise, or the caller loses the report
    for steps 1-3 that already succeeded."""
    fp = _enrol(store, "alpha", "1111111111111111")

    def _boom(device_fp):
        raise OSError("disk full")

    monkeypatch.setattr(DR, "mark_revoked", _boom)

    result = DU.unlink_device(fp, device_store=store)

    assert result["registry_marked"] is False
    # Everything upstream of the raise still reported truthfully.
    assert result["sessions_revoked"] is True
    assert result["slots_removed"] == ["1111111111111111"]
    assert result["store_removed"] is True


def test_one_capauth_record_raising_does_not_stop_the_others(store, tmp_path, monkeypatch):
    """Important 5/6: a failing capauth record must not shield the rest from
    revocation, and the report must disambiguate partial from total failure."""
    fp = _enrol(store, "alpha", "1111111111111111")

    class _FakeDevice:
        def __init__(self, device_id):
            self.device_id = device_id

    fake_devices = [_FakeDevice("d1"), _FakeDevice("d2"), _FakeDevice("d3")]
    revoked: list[str] = []

    def fake_list_devices(subject, *, base_dir=None, include_revoked=True):
        return fake_devices

    def fake_revoke(device_id, reason, *, base_dir=None):
        if device_id == "d2":
            raise RuntimeError("capauth hiccup")
        revoked.append(device_id)

    monkeypatch.setattr("capauth.pairing.default_base_dir", lambda: tmp_path)
    monkeypatch.setattr("capauth.pairing.list_devices", fake_list_devices)
    monkeypatch.setattr("capauth.pairing.revoke", fake_revoke)

    result = DU.unlink_device(fp, device_store=store)

    assert revoked == ["d1", "d3"]  # d2's failure did not stop d3
    assert result["capauth_revoked"] is True
    assert result["capauth_records_failed"] == 1
