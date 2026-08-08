"""The three Linked Devices endpoints, driven through a real FastAPI app."""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import device_registry as DR
from skchat import operator_auth as OA
from skchat import pq_prekeys as PQ
from skchat.device_routes import register_device_routes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (which is read
    # nowhere). Getting this wrong writes real slot files into the operator's live
    # ~/.skchat/pqc/peers/chef/ and, in the unlink tests, DELETES real ones.
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return OA.DeviceStore(tmp_path / "devices.json")


@pytest.fixture
def client(store):
    app = FastAPI()
    register_device_routes(app, device_store=store)
    return TestClient(app, client=("127.0.0.1", 12345))


def _enrol(store, seed: str, key_id: str) -> str:
    pub = base64.b64encode(seed.encode().ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label=seed, label_source="client", platform="app", user_agent="UA")
    # This suite is about list/rename/unlink behavior for already-linked
    # devices, not the Phase 3 approval gate (see test_device_approval.py), so
    # approve it the way an operator would.
    DR.set_approved(fp, True)
    PQ.store_peer_bundle(
        "chef",
        {"suite": "x25519-mlkem768", "hybrid_public_hex": key_id + "00" * 8, "key_id": key_id},
    )
    DR.record_publish(fp, key_id)
    return fp


def _as(fp: str) -> dict:
    return {"Authorization": f"Bearer {OA.mint_operator_session(device_fp=fp)}"}


def test_list_marks_the_calling_device_as_current(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    other = _enrol(store, "otherdev", "2222222222222222")

    r = client.get("/api/v1/operator/devices", headers=_as(me))
    assert r.status_code == 200, r.text
    rows = {d["device_fp"]: d for d in r.json()["devices"]}
    assert rows[me]["is_current"] is True
    assert rows[other]["is_current"] is False
    assert rows[me]["label"] == "mydevice"
    assert rows[other]["key_ids"] == ["2222222222222222"]


def test_a_non_operator_is_refused(client, store):
    _enrol(store, "mydevice", "1111111111111111")
    assert client.get("/api/v1/operator/devices").status_code in (401, 403)


def test_a_non_operator_is_refused_on_delete(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    assert client.delete(f"/api/v1/operator/devices/{me}").status_code in (401, 403)
    assert store.is_enrolled(me) is True


def test_a_non_operator_is_refused_on_unlink_others(client, store):
    _enrol(store, "mydevice", "1111111111111111")
    assert client.post("/api/v1/operator/devices/unlink-others").status_code in (401, 403)


def test_delete_refuses_a_shared_token_caller_with_no_session(client, store):
    # A caller authenticated with the shared operator token (not a session
    # Bearer) passes _require_operator but has no resolvable device_fp, so
    # _current_device_fp returns "". Before the Critical-1 fix this let a
    # shared-token caller DELETE any device, including looping over every one.
    me = _enrol(store, "mydevice", "1111111111111111")
    r = client.delete(
        f"/api/v1/operator/devices/{me}",
        headers={"Authorization": "Bearer op-secret"},
    )
    assert r.status_code == 400
    assert store.is_enrolled(me) is True


def test_delete_refuses_a_loopback_caller_with_no_session(client, store, monkeypatch):
    # With no shared operator token configured, _require_operator falls back to
    # trusting the loopback/tailnet caller outright -- no auth headers at all.
    # That caller still has no resolvable device_fp, so DELETE must 400 rather
    # than silently treat it as authorized to unlink anything.
    me = _enrol(store, "mydevice", "1111111111111111")
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    r = client.delete(f"/api/v1/operator/devices/{me}")
    assert r.status_code == 400
    assert store.is_enrolled(me) is True


def test_unlink_removes_the_other_device_and_its_slot(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    other = _enrol(store, "otherdev", "2222222222222222")

    r = client.delete(f"/api/v1/operator/devices/{other}", headers=_as(me))
    assert r.status_code == 200, r.text
    assert r.json()["slots_removed"] == ["2222222222222222"]
    assert [b["key_id"] for b in PQ.load_peer_bundles("chef")] == ["1111111111111111"]
    assert store.is_enrolled(other) is False


def test_a_device_cannot_unlink_itself(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    r = client.delete(f"/api/v1/operator/devices/{me}", headers=_as(me))
    assert r.status_code == 400
    assert store.is_enrolled(me) is True  # nothing happened


def test_unlink_of_an_unknown_device_is_404(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    r = client.delete("/api/v1/operator/devices/deadbeefdeadbeef", headers=_as(me))
    assert r.status_code == 404


def test_unlink_others_spares_the_caller(client, store):
    me = _enrol(store, "mydevice", "1111111111111111")
    a = _enrol(store, "deviceaa", "2222222222222222")
    b = _enrol(store, "devicebb", "3333333333333333")

    r = client.post("/api/v1/operator/devices/unlink-others", headers=_as(me))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["unlinked"]) == sorted([a, b])
    assert store.is_enrolled(me) is True
    assert store.is_enrolled(a) is False and store.is_enrolled(b) is False
    assert [d["device_fp"] for d in DR.list_devices()] == [me]


def test_unlink_others_surfaces_a_degraded_report(client, store):
    # A device whose registry row has no key_ids (never published, or predates
    # the registry) makes unlink_device() report registry_had_no_slots=True.
    # Task 7's report fields exist precisely so that is visible, not silently
    # folded into a bare success -- assert unlink-others actually surfaces it.
    me = _enrol(store, "mydevice", "1111111111111111")
    pub = base64.b64encode(b"noslotdev".ljust(32, b"\0")).decode()
    no_slot_fp = store.enroll(pub)
    DR.record_enroll(
        no_slot_fp, label="noslot", label_source="client", platform="app", user_agent="UA"
    )

    r = client.post("/api/v1/operator/devices/unlink-others", headers=_as(me))
    assert r.status_code == 200, r.text
    body = r.json()
    assert no_slot_fp in body["unlinked"]
    assert no_slot_fp in body["degraded"]
    assert body["skipped"] == []
    assert body["reports"][no_slot_fp]["registry_had_no_slots"] is True


def test_unlink_others_also_unlinks_a_store_only_device_with_no_registry_row(client, store):
    """Important 2: DR.list_devices() is registry-only. A device enrolled
    before this feature existed (or enrolled with the auth gate off) has a
    live DeviceStore entry and prekey slot but no registry row, and was
    invisible to the old registry-only loop. unlink-others must still reach
    it and report it, not silently return a clean, empty result."""
    me = _enrol(store, "mydevice", "1111111111111111")
    pub = base64.b64encode(b"legacydev".ljust(32, b"\0")).decode()
    legacy_fp = store.enroll(pub)  # store-enrolled only, never DR.record_enroll'd
    PQ.store_peer_bundle(
        "chef",
        {
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "legacyslot000001" + "00" * 8,
            "key_id": "legacyslot000001",
        },
    )
    assert DR.get_device(legacy_fp) is None

    r = client.post("/api/v1/operator/devices/unlink-others", headers=_as(me))

    assert r.status_code == 200, r.text
    body = r.json()
    assert legacy_fp in body["unlinked"]
    assert legacy_fp not in body["skipped"]
    assert body["reports"][legacy_fp]["registry_had_no_slots"] is True
    assert store.is_enrolled(legacy_fp) is False
    assert store.is_enrolled(me) is True


def test_unlink_others_reports_a_vanished_device_as_skipped(client, store, monkeypatch):
    me = _enrol(store, "mydevice", "1111111111111111")
    ghost = _enrol(store, "ghostdev", "9999999999999999")

    from skchat import device_unlink as DU

    real_unlink_device = DU.unlink_device

    def _flaky(fp, *, device_store, owner="chef"):
        if fp == ghost:
            raise KeyError(fp)
        return real_unlink_device(fp, device_store=device_store, owner=owner)

    monkeypatch.setattr(DU, "unlink_device", _flaky)

    r = client.post("/api/v1/operator/devices/unlink-others", headers=_as(me))
    assert r.status_code == 200, r.text
    body = r.json()
    assert ghost in body["skipped"]
    assert ghost not in body["unlinked"]
    assert ghost not in body["reports"]


def test_every_new_route_is_capability_mapped_for_the_enforcing_pdp():
    # Live runs SKCHAT_AUTHZ_PDP=enforce, where an unmapped gated route fails
    # closed, and tests/test_dataplane_coverage.py fails CI for one.
    from skchat.dataplane_auth import CAP_PREKEY, route_capability

    assert route_capability("GET", "/api/v1/operator/devices") == CAP_PREKEY
    assert route_capability("DELETE", "/api/v1/operator/devices/abc123") == CAP_PREKEY
    assert route_capability("POST", "/api/v1/operator/devices/unlink-others") == CAP_PREKEY
