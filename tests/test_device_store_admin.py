"""DeviceStore admin surface: list and remove, needed by unlink and by reset."""

from __future__ import annotations

import base64
import json

from skchat.operator_auth import DeviceStore


def _pub(seed: str) -> str:
    return base64.b64encode(seed.encode().ljust(32, b"\0")).decode()


def test_list_fps_returns_every_enrolled_fingerprint(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    a = store.enroll(_pub("alpha"))
    b = store.enroll(_pub("bravo"))
    assert sorted(store.list_fps()) == sorted([a, b])


def test_remove_deletes_one_device_and_persists(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path)
    a = store.enroll(_pub("alpha"))
    b = store.enroll(_pub("bravo"))

    assert store.remove(a) is True
    assert store.is_enrolled(a) is False
    assert store.is_enrolled(b) is True
    # Persisted, not just in memory.
    assert list(json.loads(path.read_text()).keys()) == [b]


def test_remove_of_an_unknown_device_is_false_not_an_error(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    assert store.remove("nosuchfingerprint") is False


def test_clear_empties_the_store_and_reports_the_count(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    store.enroll(_pub("alpha"))
    store.enroll(_pub("bravo"))
    assert store.clear() == 2
    assert store.list_fps() == []


def test_two_instances_do_not_resurrect_a_removed_device(tmp_path):
    """A daemon plus a CLI, or two concurrent unlinks, open two DeviceStore
    instances over the same file. A later instance's mutation must not flush
    its stale in-memory snapshot back to disk and resurrect a device an
    earlier instance already removed."""
    path = tmp_path / "devices.json"
    first = DeviceStore(path)
    a = first.enroll(_pub("alpha"))

    # A second instance constructed after `a` exists: its own __init__
    # snapshot still has `a` enrolled.
    second = DeviceStore(path)
    assert second.is_enrolled(a) is True

    # `first` removes `a` (e.g. an unlink).
    assert first.remove(a) is True

    # `second` performs an unrelated mutation. Without a reload-before-write
    # this flushes its stale snapshot (still containing `a`) back to disk.
    b = second.enroll(_pub("bravo"))

    reread = DeviceStore(path)
    assert reread.is_enrolled(a) is False
    assert reread.is_enrolled(b) is True
