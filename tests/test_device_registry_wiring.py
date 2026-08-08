"""The registry is populated by the two real routes, not by hand."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import device_registry as DR
from skchat import operator_auth as OA


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "off")
    monkeypatch.setenv("SKCHAT_PQC_DIR", str(tmp_path / "pqc"))
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_publish_attributes_the_slot_to_the_session_that_published_it(client):
    fp = "a1b2c3d4e5f60718"
    DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")
    token = OA.mint_operator_session(device_fp=fp)

    r = client.post(
        "/api/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "f8342853f762fd88" + "00" * 8,
            "key_id": "f8342853f762fd88",
            "owner": "chef",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert DR.get_device(fp)["key_ids"] == ["f8342853f762fd88"]


def test_two_devices_publishing_land_on_their_own_registry_rows(client):
    one, two = "aa" * 8, "bb" * 8
    for fp in (one, two):
        DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")

    for fp, key_id in ((one, "1111111111111111"), (two, "2222222222222222")):
        token = OA.mint_operator_session(device_fp=fp)
        r = client.post(
            "/api/v1/prekey",
            json={
                "suite": "x25519-mlkem768",
                "hybrid_public_hex": key_id + "00" * 8,
                "key_id": key_id,
                "owner": "chef",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text

    assert DR.get_device(one)["key_ids"] == ["1111111111111111"]
    assert DR.get_device(two)["key_ids"] == ["2222222222222222"]


def test_a_publish_with_no_session_still_succeeds_and_records_nothing(client, monkeypatch):
    # Gate off: no session to attribute. The publish must still work.
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "")
    r = client.post(
        "/api/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "cc" * 16,
            "key_id": "cccccccccccccccc",
            "owner": "chef",
        },
    )
    assert r.status_code == 200, r.text
    assert DR.list_devices() == []


def test_the_recorded_key_id_is_the_sanitized_slot_id_not_the_raw_claim(client):
    # pq_prekeys sanitizes key_id into the slot filename. If the registry stored
    # the raw claim, unlink would hunt for a slot file that does not exist.
    fp = "dd" * 8
    DR.record_enroll(fp, label="L", label_source="derived", platform="web", user_agent="UA")
    token = OA.mint_operator_session(device_fp=fp)
    r = client.post(
        "/api/v1/prekey",
        json={
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "ee" * 16,
            "key_id": "../../escape/me",
            "owner": "chef",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    recorded = DR.get_device(fp)["key_ids"]
    assert recorded == ["escapeme"]
    assert ".." not in recorded[0] and "/" not in recorded[0]
