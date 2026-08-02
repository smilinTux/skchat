"""PQC multi-device fanout (Phase 1), Task 3 - multi-slot prekey store.

The single overwritable peer prekey slot becomes a per-device slot LIST: one
file per ``key_id`` under ``peers/<short>/<key_id>.json``. ``store_peer_bundle``
upserts by ``key_id`` (republishing the same device updates in place), the store
caps at ``SLOT_CAP`` distinct devices (a NEW key_id past the cap raises
``SlotCapExceeded``), ``load_peer_bundles`` returns every slot newest-first, and
``remove_peer_bundle`` retires a single slot. ``load_peer_bundle`` keeps its
single-dict back-compat contract by returning the newest slot.
"""

import pytest


@pytest.fixture()
def tmp_peers(tmp_path, monkeypatch):
    """Fresh pq_prekeys bound to an isolated SKCHAT_HOME."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    import importlib

    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


def _bundle(key_id: str, ts: int) -> dict:
    """A minimal published peer prekey bundle for device ``key_id`` at ``ts``."""
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": "00" * 16 + key_id,
        "key_id": key_id,
        "device_id": f"dev-{key_id}",
        "last_published": ts,
    }


def test_upsert_two_slots_then_load_both(tmp_peers):
    tmp_peers.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=1))
    tmp_peers.store_peer_bundle("chef", _bundle("bbbbbbbbbbbbbbbb", ts=2))
    kids = {b["key_id"] for b in tmp_peers.load_peer_bundles("chef")}
    assert kids == {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"}


def test_republish_same_key_id_updates_in_place(tmp_peers):
    tmp_peers.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=1))
    tmp_peers.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=5))
    slots = tmp_peers.load_peer_bundles("chef")
    assert len(slots) == 1 and slots[0]["last_published"] == 5


def test_cap_rejects_11th_distinct_device(tmp_peers):
    for i in range(10):
        tmp_peers.store_peer_bundle("chef", _bundle(f"{i:016x}", ts=i))
    with pytest.raises(tmp_peers.SlotCapExceeded):
        tmp_peers.store_peer_bundle("chef", _bundle("ffffffffffffffff", ts=11))


def test_remove_slot(tmp_peers):
    tmp_peers.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=1))
    assert tmp_peers.remove_peer_bundle("chef", "aaaaaaaaaaaaaaaa") is True
    assert tmp_peers.load_peer_bundles("chef") == []


def test_codec_advert_is_preserved_through_store_and_load(tmp_peers):
    # Task 9 fanout only fires when load_peer_bundles reports codec == "pqdm2";
    # _normalise_bundle must preserve the advert on store (it was being stripped).
    b = _bundle("cafe000000000000", ts=1)
    b["codec"] = "pqdm2"
    tmp_peers.store_peer_bundle("chef", b)
    slots = tmp_peers.load_peer_bundles("chef")
    assert len(slots) == 1
    assert slots[0]["codec"] == "pqdm2"
