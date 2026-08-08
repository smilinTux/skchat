"""The R1 clean cut: an operator-triggered reset, never an automatic one."""

from __future__ import annotations

import base64
import os

import pytest
from click.testing import CliRunner

from skchat import device_registry as DR
from skchat import operator_auth as OA
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
    store = OA.DeviceStore(tmp_path / "devices.json")
    pub = base64.b64encode(b"alpha".ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label="L", label_source="client", platform="app", user_agent="UA")
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": "aa" * 16, "key_id": "aaaaaaaaaaaaaaaa"},
    )
    DR.record_publish(fp, "aaaaaaaaaaaaaaaa")
    return tmp_path


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
