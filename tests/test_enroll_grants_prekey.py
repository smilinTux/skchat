"""Task 7: enrollment grants the ``skchat.prekey`` capability.

Chef's 2026-08-02 web device-link failure: an enrolled device got a valid
operator session, but ``POST /api/v1/prekey`` still returned 401/403 because the
authz PDP (``SKCHAT_AUTHZ_PDP=enforce``) had no record granting that subject the
prekey-publish capability. Authentication passed; authorization did not.

This suite drives the real handshake end to end (enroll -> challenge -> session)
and then publishes a prekey with that session, asserting the PDP now ALLOWS it.
``Path.home`` is pinned to a tmp dir so the capauth pairing/token store the grant
writes to is the SAME store ``capauth.authz.decide`` reads from (both anchor on
``default_base_dir() == Path.home()/.skcapstone``), keeping the test hermetic.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

# CapAuth signs capability tokens with gpg, and since capauth 0d412ab a signing
# failure RAISES instead of quietly producing an unsigned token that decide()
# would then have honoured. That was the right fix (an unsigned token granted
# RCE), but it means a CI runner, which has no secret key and no unlocked
# agent, cannot mint at all: every mint raises TokenSigningError and every
# capauth-gated route in this module 403s.
#
# capauth.testing is CapAuth's own shipped test seam. It fakes the gpg
# SUBPROCESS boundary only: tokens are really signed and really verified,
# in-process, without gpg. It weakens nothing, an unsigned token is still
# denied, a tampered payload is still denied, and a signature from a different
# issuer is still denied.
#
# Applied PER MODULE rather than autouse in tests/conftest.py on purpose.
# tests/test_dataplane_audience_token.py and tests/test_audience_mint_endpoint.py
# generate a real ephemeral gpg key and sign end to end with no stubbing at all.
# A directory-wide autouse stub would silently reach those too and convert
# genuine coverage of the real signing path into coverage of the stub, which is
# the exact "passes for the wrong reason" failure this seam is meant to avoid.
from capauth.testing import stub_token_signing  # noqa: F401
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import operator_auth as oa
from skchat.operator_auth_routes import register_operator_auth_routes

pytestmark = pytest.mark.usefixtures("stub_token_signing")


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
    # Pin the capauth storage root (pairing devices + capability tokens) AND the
    # prekey store into tmp so the grant and the PDP read the same hermetic home.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)  # loopback-allowed operator
    monkeypatch.delenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", raising=False)
    # The gap being fixed only bites when the plane authenticates AND the authz
    # PDP governs; enforce is where an ungranted-but-authenticated subject 403s.
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "enforce")

    app = FastAPI()
    register_operator_auth_routes(app, device_store=oa.DeviceStore(tmp_path / "d.json"))
    app.include_router(daemon_proxy.router)
    # _require_operator falls back to loopback/tailnet-only with no shared token;
    # TestClient's default host is "testclient", so pin it to 127.0.0.1.
    return TestClient(app, client=("127.0.0.1", 12345))


def _enroll_and_session(client) -> str:
    """Run the full enroll -> challenge -> session handshake; return the token."""
    priv, pub = _kp()
    w = client.post("/api/v1/auth/enroll/open").json()
    e = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub,
            "window_nonce": w["window_nonce"],
            "sig": _sig(priv, _canon({"nonce": w["window_nonce"], "device_pubkey": pub})),
        },
    )
    assert e.status_code == 200, e.text
    fp = e.json()["device_fp"]
    assert e.json()["approved"] is False  # Phase 3: a fresh fp always lands pending
    # Simulate the operator approving from the CLI (or another already-linked
    # device): this suite is about the prekey-publish grant, not the approval
    # gate itself (see test_device_approval.py).
    from skchat import device_registry as DR

    DR.set_approved(fp, True)
    ch = client.get("/api/v1/auth/challenge").json()
    ssig = _sig(priv, _canon({"nonce": ch["nonce"], "device_fp": fp}))
    r = client.post(
        "/api/v1/auth/session", json={"device_fp": fp, "nonce": ch["nonce"], "sig": ssig}
    )
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


def test_enrolled_device_can_publish_prekey(client):
    """enroll -> session -> POST /prekey succeeds (was 401/403 before the grant)."""
    token = _enroll_and_session(client)
    resp = client.post(
        "/api/v1/prekey",
        headers={"Authorization": f"Bearer {token}"},
        json={"suite": "x25519-mlkem768", "hybrid_public_hex": "ab" * 32, "key_id": "abcd1234"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_never_enrolled_session_is_still_denied(client):
    """A well-formed operator session for a device that never enrolled has no
    grant, so the enforce-mode PDP must still 403 it (proves the grant, not the
    JWT alone, is what authorizes prekey publish)."""
    ghost = oa.mint_operator_session(device_fp="0000never0000", ttl=60)
    resp = client.post(
        "/api/v1/prekey",
        headers={"Authorization": f"Bearer {ghost}"},
        json={"suite": "x25519-mlkem768", "hybrid_public_hex": "cd" * 32, "key_id": "deadbeef"},
    )
    assert resp.status_code == 403, resp.text
