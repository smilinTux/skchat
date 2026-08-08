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
    """Two DeviceStore instances over the same file within ONE process (e.g.
    two sequential unlinks sharing one daemon's store) must not let a later
    instance's stale in-memory snapshot flush back to disk and resurrect a
    device an earlier instance already removed. This closes the permanent,
    same-process resurrection; it does not by itself make the file safe to
    share across separate OS processes (that would need a file lock held for
    the whole reload-mutate-write span, not just the final write)."""
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


def test_reload_degrades_on_corrupt_file_instead_of_raising(tmp_path, caplog):
    """A file corrupted after construction (a torn write from something else,
    manual tampering) must not turn a mutation into a hard failure. Before the
    reload-before-mutate fix, only __init__ ever parsed the file, so a
    post-startup corruption self-healed on the next write; the reload must
    keep that self-healing property rather than raising."""
    path = tmp_path / "devices.json"
    store = DeviceStore(path)
    a = store.enroll(_pub("alpha"))

    path.write_text("{not valid json")

    with caplog.at_level("WARNING"):
        # remove() must not raise; it keeps operating on its last-known-good
        # in-memory state rather than clobbering it with an empty map.
        assert store.remove(a) is True
    assert any("unreadable" in rec.message for rec in caplog.records)

    # The store persisted sanely afterward: the write went through and
    # overwrote the corrupt content with valid JSON.
    reread = DeviceStore(path)
    assert reread.is_enrolled(a) is False
