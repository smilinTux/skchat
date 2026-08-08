"""Device registry store: the join table between device_fp, key_id and metadata."""

from __future__ import annotations

import json

import pytest

from skchat import device_registry as DR


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    yield tmp_path


def test_record_enroll_creates_a_row_with_metadata():
    DR.record_enroll(
        "a1b2c3d4e5f60718",
        label="Pixel 8",
        label_source="client",
        platform="android",
        user_agent="Dart/3.5 (dart:io)",
    )
    rows = DR.list_devices()
    assert len(rows) == 1
    row = rows[0]
    assert row["device_fp"] == "a1b2c3d4e5f60718"
    assert row["label"] == "Pixel 8"
    assert row["label_source"] == "client"
    assert row["platform"] == "android"
    assert row["key_ids"] == []
    assert row["revoked"] is False
    assert row["enrolled_at"] > 0
    assert row["last_seen"] >= row["enrolled_at"]


def test_record_publish_attaches_a_key_id_without_duplicating():
    DR.record_enroll("aa" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR.record_publish("aa" * 8, "f8342853f762fd88")
    DR.record_publish("aa" * 8, "f8342853f762fd88")  # republish, same slot
    DR.record_publish("aa" * 8, "1111111111111111")  # a second slot
    row = DR.get_device("aa" * 8)
    assert row["key_ids"] == ["f8342853f762fd88", "1111111111111111"]


def test_record_publish_for_an_unknown_device_is_a_no_op_not_a_crash():
    # A publish can arrive from a device enrolled before the registry existed,
    # or with the auth gate off. It must never 500 the publish route.
    DR.record_publish("ff" * 8, "abc")
    assert DR.get_device("ff" * 8) is None


def test_reenrolling_an_existing_device_preserves_its_key_ids(tmp_path, monkeypatch):
    """Correlation integrity: a re-enroll must keep ids whose slot is still LIVE.

    This is what lets a later unlink still find the device's prekey slots. The
    slot file has to actually exist for that to be true: ``record_enroll`` prunes
    ids with no file on disk, because unlink-then-relink would otherwise carry
    forward ids unlink had just deleted (see test_registry_stale_key_ids.py).
    """
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (read nowhere).
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys as PQ

    PQ.store_peer_bundle(
        "chef",
        {
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "f8342853f762fd88" + "00" * 8,
            "key_id": "f8342853f762fd88",
        },
    )
    DR.record_enroll("11" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR.record_publish("11" * 8, "f8342853f762fd88")
    DR.record_enroll("11" * 8, label="L2", label_source="client", platform="web", user_agent="UA2")
    row = DR.get_device("11" * 8)
    assert row["key_ids"] == ["f8342853f762fd88"]


def test_reenrolling_a_revoked_device_clears_revoked_and_it_reappears():
    DR.record_enroll("22" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.mark_revoked("22" * 8) is True
    assert DR.list_devices() == []
    DR.record_enroll("22" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    row = DR.get_device("22" * 8)
    assert row["revoked"] is False
    assert [r["device_fp"] for r in DR.list_devices()] == ["22" * 8]


def test_reenrolling_refreshes_the_metadata():
    DR.record_enroll(
        "33" * 8, label="Old Label", label_source="derived", platform="web", user_agent="UA"
    )
    DR.record_enroll(
        "33" * 8,
        label="New Label",
        label_source="client",
        platform="android",
        user_agent="Dart/3.5",
    )
    row = DR.get_device("33" * 8)
    assert row["label"] == "New Label"
    assert row["label_source"] == "client"
    assert row["platform"] == "android"
    assert row["user_agent"] == "Dart/3.5"


def test_mark_revoked_hides_the_row_by_default_but_keeps_it_for_audit():
    DR.record_enroll("bb" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.mark_revoked("bb" * 8) is True
    assert DR.list_devices() == []
    kept = DR.list_devices(include_revoked=True)
    assert len(kept) == 1 and kept[0]["revoked"] is True
    assert DR.mark_revoked("nosuchdevice") is False


def test_a_corrupt_registry_degrades_to_empty_never_raises():
    DR.registry_path().parent.mkdir(parents=True, exist_ok=True)
    DR.registry_path().write_text("{not json at all")
    assert DR.list_devices() == []


def test_clear_all_empties_the_store_and_reports_the_count():
    DR.record_enroll("cc" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR.record_enroll("dd" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.clear_all() == 2
    assert DR.list_devices(include_revoked=True) == []


def test_touch_bumps_last_seen_only():
    DR.record_enroll("ee" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    before = DR.get_device("ee" * 8)
    DR.touch("ee" * 8)
    after = DR.get_device("ee" * 8)
    assert after["last_seen"] >= before["last_seen"]
    assert after["enrolled_at"] == before["enrolled_at"]


def test_the_stored_file_is_valid_json_keyed_by_device_fp():
    DR.record_enroll("0f" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    data = json.loads(DR.registry_path().read_text())
    assert list(data.keys()) == ["0f" * 8]


def test_touch_throttled_writes_at_most_once_per_window(monkeypatch):
    DR.record_enroll("ab" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    DR._last_touch.clear()
    assert DR.touch_throttled("ab" * 8) is True
    assert DR.touch_throttled("ab" * 8) is False  # inside the window, no second write
    DR._last_touch["ab" * 8] = 0.0  # pretend the window elapsed
    assert DR.touch_throttled("ab" * 8) is True


def test_touch_throttled_on_an_unknown_device_is_harmless():
    DR._last_touch.clear()
    assert DR.touch_throttled("99" * 8) is False or DR.get_device("99" * 8) is None
