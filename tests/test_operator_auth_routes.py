"""Tests for the /api/v1/auth/* operator device-key auth handshake routes.

Ships dark: exercises enroll (window-gated) + challenge/session (device-sig
gated) end to end, plus rejection of unenrolled devices and nonce replay.
Nothing is wired to gate other routes yet, that is a later task.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from capauth.pairing import operator_session as oa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import operator_auth_routes as oar
from skchat.operator_auth_routes import register_operator_auth_routes
from skchat.pairing_gate import PairingGate


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
    # Enrollment now grants the device its skchat.prekey capability into the
    # capauth store (default_base_dir() == Path.home()/.skcapstone); pin home to
    # tmp so these tests never write grant artifacts into the real ~/.skcapstone.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)  # loopback-allowed operator
    # The enrollment PairingGate is a MODULE GLOBAL carrying a rolling
    # accept-attempt rate limit (10 / 60s) shared across every test in the whole
    # suite. Without a per-test reset, unrelated modules' enrollments accumulate
    # in the same 60s window and throttle a later test's enroll (device then
    # never enrolls, and its session 401s). Give each test a fresh gate so the
    # routes stay isolated.
    monkeypatch.setattr(oar, "_pairing", PairingGate(max_accepts_per_window=1))
    app = FastAPI()
    register_operator_auth_routes(app, device_store=oa.DeviceStore(tmp_path / "d.json"))
    # _require_operator falls back to loopback/tailnet-only when no shared
    # operator token is set; TestClient's default ASGI client host is
    # "testclient", not loopback, so pin it to 127.0.0.1 like the other
    # _require_operator-gated route tests in this suite (e.g.
    # test_join_link.py, test_call_routes.py).
    return TestClient(app, client=("127.0.0.1", 12345))


def _enroll_and_session(client):
    """Run the full enroll -> challenge -> session handshake and return the
    session route's raw Response (helper for the response-shape tests)."""
    priv, pub = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    sig = _sig(priv, _canon({"nonce": w["window_nonce"], "device_pubkey": pub}))
    e = client.post(
        "/api/v1/auth/enroll",
        json={"device_pubkey": pub, "window_nonce": w["window_nonce"], "sig": sig},
    )
    fp = e.json()["device_fp"]
    # Phase 3: a fresh fp lands pending and cannot mint a session. These tests
    # are about the session handshake response shape, not the approval gate
    # itself (see test_device_approval.py), so approve the way an operator
    # would.
    from skchat import device_registry as DR

    DR.set_approved(fp, True)
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": fp}))
    return client.post(
        "/api/v1/auth/session",
        json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig},
    )


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
    assert e.json()["approved"] is False  # Phase 3: a fresh fp always lands pending
    from skchat import device_registry as DR

    DR.set_approved(fp, True)
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": fp}))
    r = client.post(
        "/api/v1/auth/session", json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig}
    )
    assert r.status_code == 200
    assert oa.verify_operator_session(r.json()["session_token"]).device_fp == fp


def test_session_echoes_default_issuer_policy_hs256(client, monkeypatch):
    # CR-3.4 PR4/P6: the session response carries an issuer_policy field the
    # client reads to decide which credential to attach. Default is hs256
    # (today's behavior; the client attaches the HS256 session).
    monkeypatch.delenv("SKCHAT_OPERATOR_ISSUER_POLICY", raising=False)
    r = _enroll_and_session(client)
    assert r.status_code == 200
    assert r.json()["issuer_policy"] == "hs256"


def test_session_echoes_configured_issuer_policy(client, monkeypatch):
    # Flipping the unit env to prefer-audience is how Chef drives Phase 3; the
    # server echoes it so no app rebuild is needed.
    monkeypatch.setenv("SKCHAT_OPERATOR_ISSUER_POLICY", "prefer-audience")
    r = _enroll_and_session(client)
    assert r.json()["issuer_policy"] == "prefer-audience"


def test_session_normalizes_unknown_issuer_policy_to_hs256(client, monkeypatch):
    # A typo must never silently disable auth: unknown -> the safe hs256 default.
    monkeypatch.setenv("SKCHAT_OPERATOR_ISSUER_POLICY", "banana")
    r = _enroll_and_session(client)
    assert r.json()["issuer_policy"] == "hs256"


def test_session_rejects_unenrolled_device(client):
    priv, _pub = _kp()
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": "deadbeef"}))
    r = client.post(
        "/api/v1/auth/session",
        json={"device_fp": "deadbeef", "nonce": ch["nonce"], "sig": ssig},
    )
    assert r.status_code == 401


def test_second_enroll_in_same_window_is_rejected(client):
    # Operator enrollment is one device per explicit window
    # (PairingGate(max_accepts_per_window=1)), not the pairing-gate default of 3.
    priv1, pub1 = _kp()
    priv2, pub2 = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    first = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub1,
            "window_nonce": w["window_nonce"],
            "sig": _sig(priv1, _canon({"nonce": w["window_nonce"], "device_pubkey": pub1})),
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub2,
            "window_nonce": w["window_nonce"],
            "sig": _sig(priv2, _canon({"nonce": w["window_nonce"], "device_pubkey": pub2})),
        },
    )
    assert second.status_code == 401


def test_enroll_rejects_tampered_sig(client):
    priv, pub = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    # Sign a DIFFERENT payload than the one the server will canonicalize and
    # verify against, so the signature does not match: proves tampered/mismatched
    # signatures are rejected rather than silently accepted.
    bad_sig = _sig(priv, _canon({"nonce": w["window_nonce"], "device_pubkey": "not-" + pub}))
    r = client.post(
        "/api/v1/auth/enroll",
        json={"device_pubkey": pub, "window_nonce": w["window_nonce"], "sig": bad_sig},
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
    from skchat import device_registry as DR

    DR.set_approved(fp, True)
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
