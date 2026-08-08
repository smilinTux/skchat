"""Phase 3 (approval-to-link): a new device lands pending and can do nothing
-- not mint a session, not publish a prekey -- until an already-approved
device or the CLI approves it.

THE THING THAT MUST NOT GO WRONG: the operator's 3 live devices have registry
rows with no ``approved`` key at all. A row with no ``approved`` key MUST be
read as approved, or all 3 lose the ability to authenticate at once with no
approved device left to approve from. That case is proven first, explicitly.
"""

from __future__ import annotations

import base64

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import device_registry as DR
from skchat import operator_auth as OA
from skchat import pq_prekeys as PQ
from skchat.cli import main as cli
from skchat.device_routes import register_device_routes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_OPERATOR_DEVICES", str(tmp_path / "devices.json"))
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (which is read
    # nowhere). Getting this wrong writes real slot files into the operator's live
    # ~/.skchat/pqc/peers/chef/, and in the deny (unlink) tests, DELETES real ones.
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return OA.DeviceStore(tmp_path / "devices.json")


@pytest.fixture
def client(store):
    app = FastAPI()
    register_device_routes(app, device_store=store)
    # This repo's `guest._require_operator` loopback fallback requires the
    # TestClient's reported peer to be a private-IP; TestClient's default ASGI
    # client host is "testclient", not loopback, so every request 403s before
    # reaching the route without this pin.
    return TestClient(app, client=("127.0.0.1", 12345))


def _enrol(store, seed: str, *, approved: bool) -> str:
    """Enrol a device directly against the registry, at the given approval state."""
    pub = base64.b64encode(seed.encode().ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label=seed, label_source="client", platform="app", user_agent="UA")
    if approved:
        DR.set_approved(fp, True)
    return fp


def _as(fp: str) -> dict:
    return {"Authorization": f"Bearer {OA.mint_operator_session(device_fp=fp)}"}


# --------------------------------------------------------------------------- #
# THE THING THAT MUST NOT GO WRONG: absence of the `approved` key is approved.
# --------------------------------------------------------------------------- #


def test_a_row_with_no_approved_key_reads_as_approved():
    """The migration case. The operator's 3 live devices have rows written
    before Phase 3 existed, with no `approved` key at all. Misreading that
    absence as pending would strand all 3 with no approved device left to
    approve from."""
    row = {
        "device_fp": "livedevice0000aa",
        "label": "Pre-existing device",
        "label_source": "client",
        "platform": "app",
        "user_agent": "UA",
        "enrolled_at": 1.0,
        "last_seen": 1.0,
        "key_ids": [],
        "revoked": False,
        # deliberately NO "approved" key
    }
    assert "approved" not in row
    assert DR.is_approved(row) is True


def test_guest_is_device_approved_reads_a_missing_key_as_approved(store):
    fp = store.enroll(base64.b64encode(b"legacy".ljust(32, b"\0")).decode())
    DR.record_enroll(fp, label="legacy", label_source="derived", platform="web", user_agent="UA")
    # Simulate a pre-Phase-3 row by stripping the key the way a real one never
    # had it in the first place.
    row = DR.get_device(fp)
    assert "approved" in row  # record_enroll always writes it going forward
    del row["approved"]
    data = DR._load()
    data[fp] = row
    DR._save(data)

    from skchat import guest as G

    assert G.is_device_approved(fp) is True


def test_a_device_with_no_registry_row_at_all_reads_as_approved(store):
    """A device enrolled before the registry existed (or with the gate off)
    has no row at all, not even a legacy one. Still approved by default."""
    from skchat import guest as G

    assert DR.get_device("neverregisteredaa") is None
    assert G.is_device_approved("neverregisteredaa") is True


def test_a_pre_phase3_devices_session_still_verifies(store):
    """End-to-end version of the above: a session for a device whose row
    predates `approved` must still verify."""
    fp = store.enroll(base64.b64encode(b"livebox".ljust(32, b"\0")).decode())
    DR.record_enroll(fp, label="livebox", label_source="derived", platform="web", user_agent="UA")
    row = DR.get_device(fp)
    del row["approved"]
    data = DR._load()
    data[fp] = row
    DR._save(data)

    token = OA.mint_operator_session(device_fp=fp)
    assert OA.verify_operator_session(token).device_fp == fp


# --------------------------------------------------------------------------- #
# A brand new enrollment lands pending.
# --------------------------------------------------------------------------- #


def test_a_new_enrollment_lands_pending():
    DR.record_enroll(
        "aa" * 8, label="Fresh device", label_source="client", platform="app", user_agent="UA"
    )
    row = DR.get_device("aa" * 8)
    assert row["approved"] is False


def test_a_pending_device_cannot_mint_or_verify_a_session(store):
    fp = _enrol(store, "pending1", approved=False)
    token = OA.mint_operator_session(device_fp=fp)
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token)


def test_approving_lets_a_pending_device_authenticate(store):
    fp = _enrol(store, "pending2", approved=False)
    token = OA.mint_operator_session(device_fp=fp)
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token)

    assert DR.set_approved(fp, True) is True

    assert OA.verify_operator_session(token).device_fp == fp


def test_reenrolling_an_approved_fp_keeps_it_approved():
    DR.record_enroll("bb" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.set_approved("bb" * 8, True) is True
    DR.record_enroll("bb" * 8, label="L2", label_source="client", platform="web", user_agent="UA2")
    row = DR.get_device("bb" * 8)
    assert row["approved"] is True


def test_reenrolling_a_new_fp_lands_pending_even_if_another_fp_is_approved():
    DR.record_enroll(
        "cc" * 8, label="Approved one", label_source="derived", platform="web", user_agent="UA"
    )
    DR.set_approved("cc" * 8, True)

    DR.record_enroll(
        "dd" * 8, label="Brand new", label_source="derived", platform="web", user_agent="UA"
    )
    assert DR.get_device("cc" * 8)["approved"] is True
    assert DR.get_device("dd" * 8)["approved"] is False


# --------------------------------------------------------------------------- #
# A pending device can never publish a prekey slot: publishing is itself an
# authenticated call, so no separate quarantine mechanism is needed.
# --------------------------------------------------------------------------- #


@pytest.fixture
def publish_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "off")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_a_pending_device_cannot_publish_a_prekey(publish_client):
    fp = "ee" * 8
    DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")
    assert DR.get_device(fp)["approved"] is False
    token = OA.mint_operator_session(device_fp=fp)

    r = publish_client.post(
        "/api/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "f8342853f762fd88" + "00" * 8,
            "key_id": "f8342853f762fd88",
            "owner": "chef",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401, r.text
    assert DR.get_device(fp)["key_ids"] == []


def test_an_approved_devices_publish_still_works(publish_client):
    fp = "ff" * 8
    DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")
    DR.set_approved(fp, True)
    token = OA.mint_operator_session(device_fp=fp)

    r = publish_client.post(
        "/api/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "1111111111111111" + "00" * 8,
            "key_id": "1111111111111111",
            "owner": "chef",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert DR.get_device(fp)["key_ids"] == ["1111111111111111"]


# --------------------------------------------------------------------------- #
# The pending list.
# --------------------------------------------------------------------------- #


def test_list_pending_shows_only_unapproved_unrevoked_rows():
    DR.record_enroll(
        "11" * 8, label="Pending", label_source="derived", platform="web", user_agent="UA"
    )
    DR.record_enroll(
        "22" * 8, label="Approved", label_source="derived", platform="web", user_agent="UA"
    )
    DR.set_approved("22" * 8, True)
    DR.record_enroll(
        "33" * 8, label="Denied", label_source="derived", platform="web", user_agent="UA"
    )
    DR.mark_revoked("33" * 8)

    pending_fps = [row["device_fp"] for row in DR.list_pending()]
    assert pending_fps == ["11" * 8]


def test_get_pending_route_lists_the_pending_device(client, store):
    approved = _enrol(store, "approved-caller", approved=True)
    pending = _enrol(store, "pending-caller", approved=False)

    r = client.get("/api/v1/operator/devices/pending", headers=_as(approved))
    assert r.status_code == 200, r.text
    rows = r.json()["devices"]
    assert [row["device_fp"] for row in rows] == [pending]
    assert rows[0]["approved"] is False


def test_pending_route_is_refused_for_a_non_operator(client, store):
    _enrol(store, "pending-caller", approved=False)
    r = client.get("/api/v1/operator/devices/pending")
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Approve.
# --------------------------------------------------------------------------- #


def test_approve_route_approves_the_device(client, store):
    approver = _enrol(store, "approver", approved=True)
    pending = _enrol(store, "newbie", approved=False)

    r = client.post(f"/api/v1/operator/devices/{pending}/approve", headers=_as(approver))
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True
    assert DR.get_device(pending)["approved"] is True

    # And the formerly-pending device can now actually authenticate.
    token = OA.mint_operator_session(device_fp=pending)
    assert OA.verify_operator_session(token).device_fp == pending


def test_approve_of_an_unknown_device_is_404(client, store):
    approver = _enrol(store, "approver", approved=True)
    r = client.post("/api/v1/operator/devices/deadbeefdeadbeef/approve", headers=_as(approver))
    assert r.status_code == 404


def test_approve_is_refused_for_a_non_operator(client, store):
    pending = _enrol(store, "newbie", approved=False)
    r = client.post(f"/api/v1/operator/devices/{pending}/approve")
    assert r.status_code in (401, 403)


def test_approve_refuses_a_shared_token_caller_with_no_session(client, store):
    # Mirrors the DELETE route's Critical-1 guard: a shared-token caller has no
    # resolvable device_fp, so it must not be able to vouch for a new device.
    pending = _enrol(store, "newbie", approved=False)
    r = client.post(
        f"/api/v1/operator/devices/{pending}/approve",
        headers={"Authorization": "Bearer op-secret"},
    )
    assert r.status_code == 400
    assert DR.get_device(pending)["approved"] is False


# --------------------------------------------------------------------------- #
# Deny: a full unlink.
# --------------------------------------------------------------------------- #


def test_deny_performs_a_full_unlink(client, store):
    approver = _enrol(store, "approver", approved=True)
    pending = _enrol(store, "sketchy", approved=False)
    PQ.store_peer_bundle(
        "chef",
        {
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "2222222222222222" + "00" * 8,
            "key_id": "2222222222222222",
        },
    )
    DR.record_publish(pending, "2222222222222222")

    r = client.post(f"/api/v1/operator/devices/{pending}/deny", headers=_as(approver))
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["slots_removed"] == ["2222222222222222"]
    assert report["store_removed"] is True
    assert report["registry_marked"] is True

    # All four stores agree it is gone.
    assert store.is_enrolled(pending) is False
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == []
    assert pending not in [row["device_fp"] for row in DR.list_devices()]
    kept = DR.list_devices(include_revoked=True)
    assert any(row["device_fp"] == pending and row["revoked"] for row in kept)

    # And its session is dead.
    token = OA.mint_operator_session(device_fp=pending)
    with pytest.raises(OA.OperatorAuthError):
        OA.verify_operator_session(token)


def test_deny_of_an_unknown_device_is_404(client, store):
    approver = _enrol(store, "approver", approved=True)
    r = client.post("/api/v1/operator/devices/deadbeefdeadbeef/deny", headers=_as(approver))
    assert r.status_code == 404


def test_deny_is_refused_for_a_non_operator(client, store):
    pending = _enrol(store, "sketchy", approved=False)
    r = client.post(f"/api/v1/operator/devices/{pending}/deny")
    assert r.status_code in (401, 403)


def test_deny_refuses_to_target_the_caller_itself(client, store):
    approver = _enrol(store, "approver", approved=True)
    r = client.post(f"/api/v1/operator/devices/{approver}/deny", headers=_as(approver))
    assert r.status_code == 400
    assert store.is_enrolled(approver) is True


# --------------------------------------------------------------------------- #
# Capability mapping: SKCHAT_AUTHZ_PDP=enforce fails unmapped routes closed.
# --------------------------------------------------------------------------- #


def test_the_three_new_routes_are_capability_mapped_for_the_enforcing_pdp():
    from skchat.dataplane_auth import CAP_PREKEY, route_capability

    assert route_capability("GET", "/api/v1/operator/devices/pending") == CAP_PREKEY
    assert route_capability("POST", "/api/v1/operator/devices/abc123/approve") == CAP_PREKEY
    assert route_capability("POST", "/api/v1/operator/devices/abc123/deny") == CAP_PREKEY


# --------------------------------------------------------------------------- #
# Enroll route response tells the caller it is pending.
# --------------------------------------------------------------------------- #


def test_enroll_route_response_reports_pending_false_field(store):
    # A registry-level check of the same contract exercised end to end in
    # test_operator_auth_routes.py::test_full_enroll_then_session.
    DR.record_enroll("gg" * 8, label="L", label_source="derived", platform="web", user_agent="UA")
    row = DR.get_device("gg" * 8)
    assert row["approved"] is False


# --------------------------------------------------------------------------- #
# CLI: the bootstrap path, no session required.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "cli-registry.json"))
    monkeypatch.setenv("SKCHAT_OPERATOR_DEVICES", str(tmp_path / "cli-devices.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "cli-rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setattr("capauth.pairing.default_base_dir", lambda: tmp_path / "capauth")
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()


def test_cli_pending_lists_a_pending_device():
    DR.record_enroll(
        "hh" * 8, label="CLI pending", label_source="derived", platform="web", user_agent="UA"
    )
    result = CliRunner().invoke(cli, ["devices", "pending"])
    assert result.exit_code == 0, result.output
    assert "hh" * 8 in result.output


def test_cli_pending_reports_none_when_empty():
    result = CliRunner().invoke(cli, ["devices", "pending"])
    assert result.exit_code == 0, result.output
    assert "No devices pending" in result.output


def test_cli_approve_works_without_any_session():
    DR.record_enroll(
        "ii" * 8, label="CLI target", label_source="derived", platform="web", user_agent="UA"
    )
    assert DR.get_device("ii" * 8)["approved"] is False

    result = CliRunner().invoke(cli, ["devices", "approve", "ii" * 8])
    assert result.exit_code == 0, result.output
    assert DR.get_device("ii" * 8)["approved"] is True

    # And the device can now actually authenticate.
    token = OA.mint_operator_session(device_fp="ii" * 8)
    assert OA.verify_operator_session(token).device_fp == "ii" * 8


def test_cli_approve_of_an_unknown_device_fails():
    result = CliRunner().invoke(cli, ["devices", "approve", "nosuchdevice"])
    assert result.exit_code != 0


def test_cli_deny_performs_a_full_unlink(tmp_path):
    pub = base64.b64encode(b"clidenyme".ljust(32, b"\0")).decode()
    store = OA.DeviceStore(tmp_path / "cli-devices.json")
    fp = store.enroll(pub)
    DR.record_enroll(fp, label="CLI deny", label_source="derived", platform="web", user_agent="UA")

    result = CliRunner().invoke(cli, ["devices", "deny", fp])
    assert result.exit_code == 0, result.output
    assert DR.list_devices() == []  # revoked, hidden from the default list
    assert OA.DeviceStore(tmp_path / "cli-devices.json").is_enrolled(fp) is False


def test_cli_deny_of_an_unknown_device_fails():
    result = CliRunner().invoke(cli, ["devices", "deny", "nosuchdevice"])
    assert result.exit_code != 0
