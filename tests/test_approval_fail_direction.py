"""Which way the approval gate fails when the registry cannot answer.

Phase 3's whole premise is that holding the pasted operator token is no longer
enough to link a usable device. A device that never got an ``approved: False``
row must therefore NOT be silently trusted, or an attacker who can enroll and
also suppress the registry write walks straight past the gate.

But the opposite blanket rule is worse. ``_load`` degrades a corrupt or
unreadable registry to an empty dict, so "no row" and "I cannot read the file"
look identical from the inside. Failing closed on both would mean one corrupt
JSON file locks every device out of the node at once, with the CLI as the only
way back in.

So the two cases are distinguished:

  * registry readable, no row for this fingerprint -> NOT approved (pending),
    and the CLI lists it so it can still be approved.
  * registry missing or unreadable -> approved, because bricking every device
    over a filesystem problem is a worse failure than briefly not enforcing a
    gate that only matters against a caller who already holds the token.
"""

from __future__ import annotations

import json

import pytest

from skchat import device_registry as DR
from skchat import guest as G


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    yield


def _write(rows: dict) -> None:
    DR.registry_path().parent.mkdir(parents=True, exist_ok=True)
    DR.registry_path().write_text(json.dumps(rows))


def test_a_readable_registry_with_no_row_is_NOT_approved():
    """The security case: an unrecorded device must not be trusted by default."""
    _write({"aa" * 8: {"device_fp": "aa" * 8, "approved": True}})

    assert G.is_device_approved("ff" * 8) is False


def test_a_row_with_no_approved_key_is_approved():
    """The migration case: rows written before Phase 3 stay working."""
    _write({"bb" * 8: {"device_fp": "bb" * 8, "label": "Chrome"}})

    assert G.is_device_approved("bb" * 8) is True


def test_an_explicitly_pending_row_is_not_approved():
    _write({"cc" * 8: {"device_fp": "cc" * 8, "approved": False}})

    assert G.is_device_approved("cc" * 8) is False


def test_a_corrupt_registry_does_NOT_lock_every_device_out():
    """One bad JSON file must not brick the whole node."""
    DR.registry_path().parent.mkdir(parents=True, exist_ok=True)
    DR.registry_path().write_text("{not json at all")

    assert G.is_device_approved("aa" * 8) is True
    assert G.is_device_approved("zz" * 8) is True


def test_a_registry_that_is_not_a_dict_does_not_lock_everyone_out():
    DR.registry_path().parent.mkdir(parents=True, exist_ok=True)
    DR.registry_path().write_text("[1, 2, 3]")

    assert G.is_device_approved("aa" * 8) is True


def test_an_absent_registry_file_does_not_lock_everyone_out():
    """A node that has never written the registry is the pre-Phase-1 world."""
    assert DR.registry_path().exists() is False

    assert G.is_device_approved("aa" * 8) is True


def test_an_unreadable_registry_does_not_lock_everyone_out(monkeypatch):
    """A permissions or IO failure reads the same as corrupt: stay open."""
    _write({"aa" * 8: {"device_fp": "aa" * 8, "approved": False}})

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(type(DR.registry_path()), "read_text", _boom, raising=False)
    assert G.is_device_approved("aa" * 8) is True


def test_a_rowless_but_enrolled_device_can_still_be_approved():
    """Failing closed must not strand a device with no route back.

    A device whose registry write failed has no row, so it is pending by the
    rule above. Approving it has to work anyway, otherwise the only recovery is
    hand-editing JSON.
    """
    assert DR.set_approved("dd" * 8, True) is True
    assert G.is_device_approved("dd" * 8) is True
    row = DR.get_device("dd" * 8)
    assert row is not None and row["approved"] is True
