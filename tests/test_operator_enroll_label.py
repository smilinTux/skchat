"""R2: an enrollment label is bound into the device signature or it is not accepted."""

from __future__ import annotations

import base64
import json

import pytest
from capauth.pairing import operator_session as oa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import device_registry as DR
from skchat.operator_auth_routes import register_operator_auth_routes


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def pub_b64(key):
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(raw).decode()


def _sign(key, payload: bytes) -> str:
    der = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()
    app = FastAPI()
    register_operator_auth_routes(app, device_store=oa.DeviceStore(tmp_path / "devices.json"))
    # _require_operator falls back to loopback/tailnet-only when no shared
    # operator token is set; TestClient's default ASGI client host is
    # "testclient", not loopback, so pin it to 127.0.0.1 like the other
    # _require_operator-gated route tests in this suite (e.g.
    # test_operator_auth_routes.py, test_join_link.py, test_call_routes.py).
    return TestClient(app, client=("127.0.0.1", 12345))


def _open_window(client) -> str:
    r = client.post("/api/v1/auth/enroll/open")
    assert r.status_code == 200, r.text
    return r.json()["window_nonce"]


def test_a_signed_label_is_accepted_and_stored_as_client_sourced(client, key, pub_b64):
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64, "label": "Chef's Pixel"})
    r = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub_b64,
            "window_nonce": nonce,
            "sig": _sign(key, payload),
            "label": "Chef's Pixel",
        },
    )
    assert r.status_code == 200, r.text
    row = DR.get_device(r.json()["device_fp"])
    assert row["label"] == "Chef's Pixel"
    assert row["label_source"] == "client"


def test_a_label_not_covered_by_the_signature_is_rejected(client, key, pub_b64):
    # Signature over the OLD two-field payload, but a label rides along in the
    # body. Accepting this would let a proxy write any label it liked.
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64})
    r = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub_b64,
            "window_nonce": nonce,
            "sig": _sign(key, payload),
            "label": "Injected",
        },
    )
    assert r.status_code == 401


def test_a_tampered_label_is_rejected(client, key, pub_b64):
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64, "label": "Real"})
    r = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub_b64,
            "window_nonce": nonce,
            "sig": _sign(key, payload),
            "label": "Tampered",
        },
    )
    assert r.status_code == 401


def test_an_enroll_with_no_label_still_works_on_the_old_payload(client, key, pub_b64):
    # Backwards compatibility: the shipped web build signs two fields only.
    nonce = _open_window(client)
    payload = _canon({"nonce": nonce, "device_pubkey": pub_b64})
    r = client.post(
        "/api/v1/auth/enroll",
        json={"device_pubkey": pub_b64, "window_nonce": nonce, "sig": _sign(key, payload)},
        headers={"User-Agent": "Mozilla/5.0 Chrome/131 Safari/537.36"},
    )
    assert r.status_code == 200, r.text
    row = DR.get_device(r.json()["device_fp"])
    assert row["label_source"] == "derived"
    assert row["label"] == "Chrome"
