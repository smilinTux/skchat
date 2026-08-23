"""The R1 clean cut: an operator-triggered reset, never an automatic one."""

from __future__ import annotations

import base64
import json
import os

import pytest
from capauth.pairing import operator_session as OA
from click.testing import CliRunner

from skchat import device_registry as DR
from skchat import pq_prekeys as PQ

# The CLI's top-level click.Group is named `main` in skchat.cli, not `cli` (a
# brief for this task assumed the latter). Aliased on import so the rest of
# this test reads exactly as written against the click group under test.
from skchat.cli import main as cli


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (which is read
    # nowhere). Getting this wrong writes real slot files into the operator's live
    # ~/.skchat/pqc/peers/chef/ and, in the unlink tests, DELETES real ones.
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    # operator_auth.default_device_store_path() reads this (added in this task;
    # nothing read it before, so this isolation used to be fictional).
    monkeypatch.setenv("SKCHAT_OPERATOR_DEVICES", str(tmp_path / "devices.json"))
    # Reset now also revokes sessions and capauth grants (Critical 1), so its
    # tests need the same isolation the unlink tests already use: the guest
    # revocation DB and operator token secret so sessions can be minted and
    # verified, and capauth.pairing.default_base_dir pointed at a tmp dir so a
    # test run can never touch the operator's real ~/.skcapstone.
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setattr("capauth.pairing.default_base_dir", lambda: tmp_path / "capauth")
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    store = OA.DeviceStore(tmp_path / "devices.json")
    pub = base64.b64encode(b"alpha".ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label="L", label_source="client", platform="app", user_agent="UA")
    # This suite is about the reset flow for an already-linked device, not the
    # Phase 3 approval gate, so approve it the way an operator would.
    DR.set_approved(fp, True)
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": "aa" * 16, "key_id": "aaaaaaaaaaaaaaaa"},
    )
    DR.record_publish(fp, "aaaaaaaaaaaaaaaa")
    return tmp_path


def _enrolled_fp() -> str:
    """The device_fp of the fixture's single pre-enrolled device."""
    pub = base64.b64encode(b"alpha".ljust(32, b"\0")).decode()
    return OA.device_fingerprint(pub)


def test_reset_without_yes_refuses_and_changes_nothing():
    result = CliRunner().invoke(cli, ["devices", "reset"])
    assert result.exit_code != 0
    assert DR.list_devices(include_revoked=True) != []
    assert PQ.load_peer_bundles("chef") != []


def test_reset_with_yes_clears_every_store():
    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert DR.list_devices(include_revoked=True) == []
    assert PQ.load_peer_bundles("chef") == []
    assert OA.DeviceStore(os.environ["SKCHAT_OPERATOR_DEVICES"]).list_fps() == []


def test_reset_reports_what_it_removed():
    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])
    assert "1" in result.output  # counts are surfaced, not silent
    assert "device" in result.output.lower()


# --------------------------------------------------------------------------- #
# Critical 1: reset did only 2 of unlink_device's 4 steps (store + slots), so
# a "reset" device kept a live session and a live capauth grant. These prove
# the fix reuses the same session-revocation and capauth-revocation machinery
# unlink_device uses, not a parallel, weaker mechanism.
# --------------------------------------------------------------------------- #


def test_reset_revokes_sessions_so_a_pre_reset_token_stops_verifying():
    fp = _enrolled_fp()
    token = OA.mint_operator_session(device_fp=fp)
    assert OA.verify_operator_session(token).device_fp == fp

    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])
    assert result.exit_code == 0, result.output

    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token)


def test_reset_revokes_capauth_pairing_records_for_every_target_device(monkeypatch):
    class _FakeDevice:
        def __init__(self, device_id):
            self.device_id = device_id

    revoked: list[str] = []

    def fake_list_devices(subject, *, base_dir=None, include_revoked=True):
        return [_FakeDevice("dev-1")]

    def fake_revoke(device_id, reason, *, base_dir=None):
        revoked.append(device_id)

    monkeypatch.setattr("capauth.pairing.list_devices", fake_list_devices)
    monkeypatch.setattr("capauth.pairing.revoke", fake_revoke)

    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert revoked == ["dev-1"]
    assert "1 capauth subject(s) revoked" in result.output


def test_reset_reports_session_and_capauth_revocation_counts_truthfully():
    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])
    assert result.exit_code == 0, result.output
    # No capauth pairing store exists for this device in this test (real
    # capauth.pairing.list_devices against an empty tmp base dir finds
    # nothing), so the honest count is 0 revoked, not a false positive.
    assert "1 session(s) revoked" in result.output
    assert "0 capauth subject(s) revoked" in result.output


# --------------------------------------------------------------------------- #
# Important 3: a classical (no key_id) bundle collapses to the on-disk
# `_default` slot. The old code's `if key_id and ...` guard skipped it, so it
# survived reset while the preview count claimed it was gone.
# --------------------------------------------------------------------------- #


def test_reset_removes_and_counts_a_classical_default_slot():
    # A bundle with no key_id collapses to peers/chef/_default.json.
    PQ.store_peer_bundle("chef", {"suite": "x25519", "hybrid_public_hex": "cc" * 16})
    default_path = PQ._peer_dir("chef") / "_default.json"
    assert default_path.is_file()

    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert not default_path.exists()
    assert PQ.load_peer_bundles("chef") == []
    # The fixture's one keyed slot plus this classical slot: both removed and
    # both counted, not a preview of 2 with only 1 actually gone.
    assert "Cleared 1 enrolled device(s), 2 prekey slot(s)" in result.output


def _legacy_bundle_path(skchat_home: "os.PathLike[str]") -> "os.PathLike[str]":
    """Where a pre-multislot deployment's one flat bundle lives.

    Built the same way ``pq_prekeys.load_peer_bundles`` locates it: written
    directly here (NOT via ``store_peer_bundle``, which only ever writes the
    new per-slot shape) so the test actually seeds the legacy shape that
    ``remove_peer_bundle`` cannot see.
    """
    from pathlib import Path

    return Path(skchat_home) / "pqc" / "peers" / "chef.json"


def test_reset_with_yes_also_clears_legacy_flat_bundle(tmp_path):
    legacy_path = _legacy_bundle_path(tmp_path)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "suite": "x25519-mlkem768",
                "hybrid_public_hex": "bb" * 16,
                "key_id": "bbbbbbbbbbbbbbbb",
            }
        )
    )

    result = CliRunner().invoke(cli, ["devices", "reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert not legacy_path.exists()
    assert "2" in result.output  # the new-shape slot AND the legacy bundle
    assert PQ.load_peer_bundles("chef") == []


def test_reset_without_yes_leaves_legacy_bundle_untouched(tmp_path):
    legacy_path = _legacy_bundle_path(tmp_path)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({"key_id": "bbbbbbbbbbbbbbbb"}))

    result = CliRunner().invoke(cli, ["devices", "reset"])

    assert result.exit_code != 0
    assert legacy_path.is_file()
