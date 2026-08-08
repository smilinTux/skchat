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


def test_every_new_route_is_capability_mapped_for_the_enforcing_pdp():
    # Live runs SKCHAT_AUTHZ_PDP=enforce, where an unmapped gated route fails
    # closed, and tests/test_dataplane_coverage.py fails CI for one.
    from skchat.dataplane_auth import CAP_PREKEY, route_capability

    assert route_capability("GET", "/api/v1/operator/devices") == CAP_PREKEY
    assert route_capability("DELETE", "/api/v1/operator/devices/abc123") == CAP_PREKEY
    assert route_capability("POST", "/api/v1/operator/devices/unlink-others") == CAP_PREKEY
