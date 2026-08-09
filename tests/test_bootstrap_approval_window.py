"""The bootstrap window: how the FIRST device gets approved without a second command.

Approval-to-link means a newly enrolled device lands pending, and nothing
auto-approves. That is deliberate: the operator token is a long-lived plaintext
secret, so a leaked token alone must never be enough to link a usable device.

But it left a rough edge. Right after ``devices reset`` there are zero approved
devices, so there is nobody to approve the first one from, and the operator had
to go back to the terminal for a second command.

The resolution keeps the security property and removes the second command: the
reset ITSELF opens a short auto-approve window. Opening it requires running a
terminal command on the box, which is strictly stronger evidence than holding
the token, so a leaked token still buys nothing. The window is bounded, so the
exposure is minutes rather than "until someone notices".

What must hold:
  * a device enrolling while the window is open is approved
  * exactly ONE device gets in that way; the second still lands pending
  * an expired window approves nobody
  * no window at all approves nobody (the default state)
  * consuming or expiring the window cannot resurrect it
"""

from __future__ import annotations

import pytest

from skchat import bootstrap_window as BW


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_BOOTSTRAP_WINDOW", str(tmp_path / "bootstrap.json"))
    yield


def test_no_window_by_default_approves_nobody():
    """The default state must be closed, not open."""
    assert BW.is_open() is False
    assert BW.consume() is False


def test_an_open_window_approves_exactly_one_device():
    BW.open_window(ttl=900)
    assert BW.is_open() is True

    assert BW.consume() is True, "the first device should be let in"
    assert BW.consume() is False, "a second device must NOT ride the same window"
    assert BW.is_open() is False


def test_an_expired_window_approves_nobody():
    BW.open_window(ttl=900, now=1000.0)

    assert BW.is_open(now=1000.0 + 899) is True
    assert BW.is_open(now=1000.0 + 901) is False
    assert BW.consume(now=1000.0 + 901) is False


def test_consuming_an_expired_window_does_not_let_a_device_in():
    """The boundary that matters: expiry must beat consumption."""
    BW.open_window(ttl=60, now=500.0)
    assert BW.consume(now=600.0) is False


def test_reopening_replaces_rather_than_stacks():
    """Two resets must not leave two windows open."""
    BW.open_window(ttl=900, now=1000.0)
    BW.open_window(ttl=900, now=2000.0)
    assert BW.is_open(now=2500.0) is True
    assert BW.consume(now=2500.0) is True
    assert BW.consume(now=2500.0) is False


def test_a_corrupt_window_file_reads_as_closed():
    """Fail closed here: an unreadable window must never mean 'let anyone in'."""
    BW.window_path().parent.mkdir(parents=True, exist_ok=True)
    BW.window_path().write_text("{not json")

    assert BW.is_open() is False
    assert BW.consume() is False


def test_close_is_idempotent():
    BW.open_window(ttl=900)
    BW.close()
    BW.close()
    assert BW.is_open() is False


# ── The window driven through the REAL enroll route ────────────────────────── #
# The unit tests above prove the window's own semantics. These prove the wiring:
# that an enrollment actually consults it, that exactly one device benefits, and
# that the default (no window) still lands pending.


def _enroll_through_the_real_route(client, seed: str) -> dict:
    """Complete a real signed enrollment and return the route's JSON."""
    import base64
    import json as _json

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric import utils as au

    key = ec.generate_private_key(ec.SECP256R1())
    pub = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode()

    def canon(obj) -> bytes:
        return _json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

    def sign(payload: bytes) -> str:
        der = key.sign(payload, ec.ECDSA(hashes.SHA256()))
        r, s = au.decode_dss_signature(der)
        return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()

    opened = client.post("/api/v1/auth/enroll/open")
    assert opened.status_code == 200, opened.text
    nonce = opened.json()["window_nonce"]
    resp = client.post(
        "/api/v1/auth/enroll",
        json={
            "device_pubkey": pub,
            "window_nonce": nonce,
            "sig": sign(canon({"nonce": nonce, "device_pubkey": pub})),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def enroll_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skchat import operator_auth as OA
    from skchat.operator_auth_routes import register_operator_auth_routes

    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_operator_auth_routes(app, device_store=OA.DeviceStore(tmp_path / "devices.json"))
    # Loopback pin: _require_operator's fallback needs a private-IP peer.
    return TestClient(app, client=("127.0.0.1", 12345))


def test_with_no_window_a_new_device_lands_pending(enroll_client):
    """The default. This is the whole point of approval-to-link."""
    body = _enroll_through_the_real_route(enroll_client, "alpha")
    assert body["approved"] is False


def test_with_the_window_open_the_first_device_is_approved(enroll_client):
    BW.open_window()
    body = _enroll_through_the_real_route(enroll_client, "alpha")
    assert body["approved"] is True, "the reset-opened window should admit the first device"


def test_only_the_first_device_rides_the_window(enroll_client):
    """A single reset must not admit an attacker behind the operator's device."""
    BW.open_window()

    first = _enroll_through_the_real_route(enroll_client, "alpha")
    second = _enroll_through_the_real_route(enroll_client, "bravo")

    assert first["approved"] is True
    assert second["approved"] is False, "the window is single-use"


def test_an_expired_window_leaves_the_device_pending(enroll_client):
    BW.open_window(ttl=0)
    body = _enroll_through_the_real_route(enroll_client, "alpha")
    assert body["approved"] is False
