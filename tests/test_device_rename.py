"""PATCH /api/v1/operator/devices/{device_fp}: operator device rename."""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import device_registry as DR
from skchat import operator_auth as OA
from skchat.device_routes import register_device_routes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    # pq_prekeys._pqc_dir() reads SKCHAT_HOME, NOT SKCHAT_PQC_DIR (which is read
    # nowhere). Getting this wrong writes real slot files into the operator's live
    # ~/.skchat/pqc/peers/chef/.
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


def _enrol(store, seed: str) -> str:
    pub = base64.b64encode(seed.encode().ljust(32, b"\0")).decode()
    fp = store.enroll(pub)
    DR.record_enroll(fp, label=seed, label_source="client", platform="app", user_agent="UA")
    # This suite is about rename behavior for an already-linked device, not the
    # Phase 3 approval gate, so approve it the way an operator would.
    DR.set_approved(fp, True)
    return fp


def _as(fp: str) -> dict:
    return {"Authorization": f"Bearer {OA.mint_operator_session(device_fp=fp)}"}


def test_rename_changes_the_label_and_flips_label_source(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(
        f"/api/v1/operator/devices/{fp}", json={"label": "Dave's Laptop"}, headers=_as(fp)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "Dave's Laptop"
    assert body["label_source"] == "operator"
    assert "user_agent" not in body


def test_rename_survives_a_re_read_of_the_registry(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(
        f"/api/v1/operator/devices/{fp}", json={"label": "Kitchen Tablet"}, headers=_as(fp)
    )
    assert r.status_code == 200, r.text

    row = DR.get_device(fp)
    assert row["label"] == "Kitchen Tablet"
    assert row["label_source"] == "operator"


def test_rename_trims_whitespace(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(
        f"/api/v1/operator/devices/{fp}", json={"label": "   Office PC   "}, headers=_as(fp)
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Office PC"


def test_rename_caps_an_overlong_label_at_64_chars(client, store):
    fp = _enrol(store, "mydevice")
    long_label = "x" * 200

    r = client.patch(f"/api/v1/operator/devices/{fp}", json={"label": long_label}, headers=_as(fp))
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "x" * 64


def test_empty_label_is_400_and_changes_nothing(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(f"/api/v1/operator/devices/{fp}", json={"label": ""}, headers=_as(fp))
    assert r.status_code == 400
    assert DR.get_device(fp)["label"] == "mydevice"


def test_whitespace_only_label_is_400_and_changes_nothing(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(f"/api/v1/operator/devices/{fp}", json={"label": "   "}, headers=_as(fp))
    assert r.status_code == 400
    assert DR.get_device(fp)["label"] == "mydevice"


def test_missing_label_key_is_400_and_changes_nothing(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(f"/api/v1/operator/devices/{fp}", json={}, headers=_as(fp))
    assert r.status_code == 400
    assert DR.get_device(fp)["label"] == "mydevice"


def test_non_string_label_is_400_and_changes_nothing(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(f"/api/v1/operator/devices/{fp}", json={"label": 42}, headers=_as(fp))
    assert r.status_code == 400
    assert DR.get_device(fp)["label"] == "mydevice"


def test_unknown_fingerprint_is_404(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(
        "/api/v1/operator/devices/deadbeefdeadbeef", json={"label": "Ghost"}, headers=_as(fp)
    )
    assert r.status_code == 404


def test_a_non_operator_is_refused(client, store):
    fp = _enrol(store, "mydevice")

    r = client.patch(f"/api/v1/operator/devices/{fp}", json={"label": "Nope"})
    assert r.status_code in (401, 403)
    assert DR.get_device(fp)["label"] == "mydevice"


def test_a_caller_with_no_device_session_can_still_rename(client, store):
    # Unlike DELETE/unlink-others, rename carries no self-lockout risk, so it is
    # gated like GET: the shared operator token is enough, no session required.
    fp = _enrol(store, "mydevice")

    r = client.patch(
        f"/api/v1/operator/devices/{fp}",
        json={"label": "Renamed via shared token"},
        headers={"Authorization": "Bearer op-secret"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Renamed via shared token"


def test_the_new_route_is_capability_mapped_for_the_enforcing_pdp():
    # Live runs SKCHAT_AUTHZ_PDP=enforce, where an unmapped gated route fails
    # closed, and tests/test_dataplane_coverage.py fails CI for one.
    from skchat.dataplane_auth import CAP_PREKEY, route_capability

    assert route_capability("PATCH", "/api/v1/operator/devices/abc") == CAP_PREKEY
