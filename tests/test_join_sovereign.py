"""Local sovereign conference join (`POST /join/sovereign`).

The endpoint proves FQID ownership from a self-signed Assertion and mints a
SOVEREIGN conference token whose LiveKit identity is the PROVEN fqid (never a
caller-supplied string). These tests reuse the assertion crypto seams the way
test_fed_assertion.py / test_fed_authd.py do: a deterministic fake sign/verify
pair + the real NonceCache for the replay guard.
"""

from __future__ import annotations

import json
import time

import jwt  # PyJWT (already used by tokens.py / guest.py)
import pytest

from skchat.join_routes import JoinDenied, authorize_sovereign
from skchat.spaces.federation.assertion import (
    Assertion,
    build_signed,
    verify_signed,
)
from skchat.spaces.federation.assertion import (
    AssertionError as FedAssertionError,
)
from skchat.spaces.federation.nonce import NonceCache

_KEY, _SECRET = "test-key", "test-secret-0123456789"
_SCREENSHARE = {"screen_share", "screen_share_audio"}


# ── crypto seams (deterministic stand-ins for capauth PGP) ────────────────────


def _fake_sign(payload: bytes) -> str:
    return "SIG(" + payload.decode() + ")"


def _fake_verify_ok(payload: bytes, sig: str, pub: str) -> bool:
    return sig == "SIG(" + payload.decode() + ")"


def _signed(fqid: str, space: str, nonce: str = "n1") -> dict:
    a = Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()), nonce=nonce)
    return build_signed(a, sign=_fake_sign)


def _verify(signed: dict, **_kw) -> Assertion:
    # Run the REAL verify_signed but with injected resolver/verifier so no live
    # capauth key is needed. This exercises signature/freshness/fqid checks.
    return verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok, **_kw)


def _claims(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})


def _mint_real(identity: str, space_id: str, *, sovereign_admin: bool) -> str:
    # Mint a real conf token with dummy creds (no live SFU) so we can decode it.
    from skchat.spaces.roles import ConfRole
    from skchat.spaces.tokens import mint_conf_token

    return mint_conf_token(
        identity,
        identity.split("@")[0],
        ConfRole.SOVEREIGN,
        space_id,
        3600,
        sovereign_admin=sovereign_admin,
        api_key=_KEY,
        api_secret=_SECRET,
    )


# ── happy path: proven fqid identity + SOVEREIGN grant ────────────────────────


def test_valid_assertion_mints_sovereign_token_with_proven_identity():
    out = authorize_sovereign(
        _signed("lumina@chef.skworld", "conf-1"),
        conf_ws_url="wss://h:8443",
        _verify=_verify,
        _mint=_mint_real,
        _nonce=NonceCache(),
    )
    assert out["role"] == "sovereign"
    assert out["identity"] == "lumina@chef.skworld"
    assert out["space_id"] == "conf-1"
    assert out["conf_ws_url"] == "wss://h:8443"

    v = _claims(out["token"])["video"]
    # LiveKit identity is the PROVEN fqid (sub claim), not a caller string.
    assert _claims(out["token"])["sub"] == "lumina@chef.skworld"
    assert v["room"] == "conf-1"
    assert v["canPublish"] is True
    srcs = set(v["canPublishSources"])
    assert "camera" in srcs
    assert "microphone" in srcs
    assert _SCREENSHARE.issubset(srcs)


def test_sovereign_admin_flag_sets_room_admin():
    plain = authorize_sovereign(
        _signed("owner@chef.skworld", "conf-1", nonce="a"),
        conf_ws_url="wss://h",
        _verify=_verify,
        _mint=_mint_real,
        _nonce=NonceCache(),
    )
    admin = authorize_sovereign(
        _signed("owner@chef.skworld", "conf-1", nonce="b"),
        conf_ws_url="wss://h",
        sovereign_admin=True,
        _verify=_verify,
        _mint=_mint_real,
        _nonce=NonceCache(),
    )
    assert _claims(plain["token"])["video"].get("roomAdmin", False) is False
    assert _claims(admin["token"])["video"]["roomAdmin"] is True
    assert admin["sovereign_admin"] is True


# ── adversarial: replay, bad signature, tampered fqid ─────────────────────────


def test_replayed_nonce_is_rejected():
    nc = NonceCache()
    kwargs = dict(
        conf_ws_url="wss://h",
        _verify=_verify,
        _mint=_mint_real,
        _nonce=nc,
    )
    first = authorize_sovereign(_signed("lumina@chef.skworld", "conf-1", "dup"), **kwargs)
    assert first["identity"] == "lumina@chef.skworld"
    with pytest.raises(JoinDenied, match="replay"):
        authorize_sovereign(_signed("lumina@chef.skworld", "conf-1", "dup"), **kwargs)


def test_bad_signature_is_rejected():
    signed = _signed("lumina@chef.skworld", "conf-1")
    signed["sig"] = "SIG(forged)"  # no longer matches the canonical claim bytes
    minted: list = []
    with pytest.raises(FedAssertionError, match="signature"):
        authorize_sovereign(
            signed,
            conf_ws_url="wss://h",
            _verify=_verify,
            _mint=lambda *a, **k: minted.append(a) or "TOKEN",
            _nonce=NonceCache(),
        )
    assert minted == []  # never reached the mint seam


def test_tampered_fqid_is_rejected():
    # Keep the original signature but swap the fqid in the claim to impersonate a
    # different sovereign — the sig was computed over the OLD fqid, so verify must
    # fail and no token is minted.
    signed = _signed("rando@other.realm", "conf-1")
    swapped = dict(json.loads(signed["claim"]))
    swapped["fqid"] = "lumina@chef.skworld"
    signed["claim"] = json.dumps(swapped, sort_keys=True, separators=(",", ":"))
    minted: list = []
    with pytest.raises(FedAssertionError, match="signature"):
        authorize_sovereign(
            signed,
            conf_ws_url="wss://h",
            _verify=_verify,
            _mint=lambda *a, **k: minted.append(a) or "TOKEN",
            _nonce=NonceCache(),
        )
    assert minted == []


def test_caller_cannot_inject_identity_only_proven_fqid_used():
    # Even if the request carried an `identity` field, the minted token identity
    # is the PROVEN fqid from the assertion — the endpoint ignores any
    # caller-supplied identity (it isn't a parameter at all).
    out = authorize_sovereign(
        _signed("real@chef.skworld", "conf-1"),
        conf_ws_url="wss://h",
        _verify=_verify,
        _mint=_mint_real,
        _nonce=NonceCache(),
    )
    assert _claims(out["token"])["sub"] == "real@chef.skworld"


# ── route-level: thin parse + status mapping ──────────────────────────────────


def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skchat.join_routes import register_join_routes

    # ensure dummy creds so a real conf token can be minted through the route
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "wss://route-host:8443")
    app = FastAPI()
    register_join_routes(app)
    return TestClient(app)


def test_route_rejects_malformed_body(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/join/sovereign", content=b"not json")
    assert resp.status_code == 400


def test_route_rejects_missing_claim_or_sig(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/join/sovereign", json={"claim": "x"})
    assert resp.status_code == 400


def test_route_happy_path_returns_proven_identity_token(monkeypatch):
    # Patch the assertion verifier used inside the route so the fake crypto seam
    # is honored end-to-end (the route calls authorize_sovereign with the real
    # verify_signed default; we patch verify_signed's seams via the module).
    import skchat.join_routes as jr

    monkeypatch.setattr(jr, "verify_signed", _verify)
    client = _client(monkeypatch)
    signed = _signed("opus@chef.skworld", "conf-9")
    resp = client.post(
        "/join/sovereign",
        json={"claim": signed["claim"], "sig": signed["sig"], "sovereign_admin": True},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["identity"] == "opus@chef.skworld"
    assert out["role"] == "sovereign"
    assert out["conf_ws_url"] == "wss://route-host:8443"
    v = _claims(out["token"])["video"]
    assert _claims(out["token"])["sub"] == "opus@chef.skworld"
    assert v["roomAdmin"] is True
    assert _SCREENSHARE.issubset(set(v["canPublishSources"]))
