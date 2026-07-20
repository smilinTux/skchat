"""Tests for the /api/v1/auth/* operator device-key auth handshake routes.

Ships dark: exercises enroll (window-gated) + challenge/session (device-sig
gated) end to end, plus rejection of unenrolled devices and nonce replay.
Nothing is wired to gate other routes yet, that is a later task.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import operator_auth as oa
from skchat.operator_auth_routes import register_operator_auth_routes


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _kp():
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, base64.b64encode(spki).decode()


def _sig(priv, payload):
    der = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)  # loopback-allowed operator
    app = FastAPI()
    register_operator_auth_routes(app, device_store=oa.DeviceStore(tmp_path / "d.json"))
    # _require_operator falls back to loopback/tailnet-only when no shared
    # operator token is set; TestClient's default ASGI client host is
    # "testclient", not loopback, so pin it to 127.0.0.1 like the other
    # _require_operator-gated route tests in this suite (e.g.
    # test_join_link.py, test_call_routes.py).
    return TestClient(app, client=("127.0.0.1", 12345))


def test_full_enroll_then_session(client):
    priv, pub = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    sig = _sig(priv, _canon({"nonce": w["window_nonce"], "device_pubkey": pub}))
    e = client.post(
        "/api/v1/auth/enroll",
        json={"device_pubkey": pub, "window_nonce": w["window_nonce"], "sig": sig},
    )
    assert e.status_code == 200
    fp = e.json()["device_fp"]
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": fp}))
    r = client.post(
        "/api/v1/auth/session", json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig}
    )
    assert r.status_code == 200
    assert oa.verify_operator_session(r.json()["session_token"]).device_fp == fp


def test_session_rejects_unenrolled_device(client):
    priv, _pub = _kp()
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": "deadbeef"}))
    r = client.post(
        "/api/v1/auth/session",
        json={"device_fp": "deadbeef", "nonce": ch["nonce"], "sig": ssig},
    )
    assert r.status_code == 401


def test_session_rejects_replayed_nonce(client):
    priv, pub = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub,
            "window_nonce": w["window_nonce"],
            "sig": _sig(priv, _canon({"nonce": w["window_nonce"], "device_pubkey": pub})),
        },
    )
    fp = oa.device_fingerprint(pub)
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": fp}))
    ok = client.post(
        "/api/v1/auth/session", json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig}
    )
    assert ok.status_code == 200
    replay = client.post(
        "/api/v1/auth/session", json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig}
    )
    assert replay.status_code == 401
