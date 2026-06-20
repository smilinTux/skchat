"""Unit tests for the conf federation CLIENT (B1-fedclient).

Covers the client leg of Shape-A federation:

* sign -> POST(mock) -> parse round-trip (the happy path)
* 403 -> ConfAuthDenied, 404 -> ConfFederationError, other non-2xx -> error
* a REAL client->server round-trip against the live conf route via a FastAPI
  ``TestClient`` adapter — both the TRUSTED (token minted) and UNTRUSTED
  (fqid rejected) paths, proving the assertion contract end-to-end with no mocks
  on the verify/trust/mint side.
"""

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf.fed_client import (
    ConfAuthDenied,
    ConfFederationError,
    build_signed_conf_assertion,
    mint_remote_conf_token,
)
from skchat.conf.room import ConfRegistry
from skchat.conf.routes import register_conf_routes

_KEY, _SECRET = "test-key", "test-secret-0123456789"


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


# ── unit: sign -> POST(mock) -> parse ────────────────────────────────────────


def test_build_signed_conf_assertion_carries_room_and_fresh_nonce():
    import json

    s1 = build_signed_conf_assertion(fqid="lumina@chef.skworld", room="standup",
                                     sign=lambda p: "SIG")
    assert s1["sig"] == "SIG"
    claim = json.loads(s1["claim"])
    assert claim["fqid"] == "lumina@chef.skworld"
    assert claim["space_id"] == "standup"  # room carried in the shared space_id slot
    assert claim["nonce"]

    s2 = build_signed_conf_assertion(fqid="lumina@chef.skworld", room="standup",
                                     sign=lambda p: "SIG")
    n1 = json.loads(s1["claim"])["nonce"]
    n2 = json.loads(s2["claim"])["nonce"]
    assert n1 != n2  # fresh nonce per call -> replay-distinct


def test_mint_remote_conf_token_happy_path():
    posted = {}

    def fake_post(url, body):
        posted["url"] = url
        posted["body"] = body
        return _FakeResp(200, {
            "token": "JWT", "url": "ws://box-a:7880", "role": "participant",
            "identity": "lumina@chef.skworld", "conf_id": "conf-abc", "room": "standup",
        })

    out = mint_remote_conf_token(
        "http://box-a:8765", "standup",
        fqid="lumina@chef.skworld", post=fake_post, sign=lambda p: "SIG",
    )
    assert out["token"] == "JWT"
    assert out["url"] == "ws://box-a:7880"
    # POSTed to the conf federated-token path the server exposes
    assert posted["url"] == "http://box-a:8765/conf/standup/federated-token"
    assert posted["body"]["sig"] == "SIG"
    assert "claim" in posted["body"]


def test_mint_url_accepts_full_federated_token_url():
    posted = {}

    def fake_post(url, body):
        posted["url"] = url
        return _FakeResp(200, {"token": "JWT", "url": "ws://h"})

    mint_remote_conf_token(
        "http://box-a:8765/conf/demo/federated-token", "demo",
        fqid="a@chef.skworld", post=fake_post, sign=lambda p: "SIG",
    )
    assert posted["url"] == "http://box-a:8765/conf/demo/federated-token"


def test_mint_403_raises_auth_denied():
    with pytest.raises(ConfAuthDenied):
        mint_remote_conf_token(
            "http://box-a:8765", "standup", fqid="evil@attacker",
            post=lambda url, body: _FakeResp(403, {"detail": "not permitted"}),
            sign=lambda p: "SIG",
        )


def test_mint_404_raises_federation_error():
    with pytest.raises(ConfFederationError):
        mint_remote_conf_token(
            "http://box-a:8765", "ghost", fqid="a@chef.skworld",
            post=lambda url, body: _FakeResp(404),
            sign=lambda p: "SIG",
        )


def test_mint_non_2xx_raises_federation_error():
    with pytest.raises(ConfFederationError):
        mint_remote_conf_token(
            "http://box-a:8765", "standup", fqid="a@chef.skworld",
            post=lambda url, body: _FakeResp(500),
            sign=lambda p: "SIG",
        )


# ── real client -> server round-trip (no verify/trust/mint mocks) ────────────


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A live conf-route FastAPI app + a post seam that drives it via TestClient.

    The post seam translates the client's (url, body) into a TestClient POST so
    the REAL server-side verify_signed / TrustPolicy / mint_conf_token run.
    """
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    app = FastAPI()
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "confs.json"))
    tc = TestClient(app)

    def post(url, body):
        # url is http://server<path> — strip scheme+host, keep the path
        path = url.split("://", 1)[-1]
        path = path[path.index("/"):]
        return tc.post(path, json=body)

    return tc, post


def _create_conf(tc, slug="standup"):
    r = tc.post("/conf/create", json={"host_fqid": "lumina@chef.skworld",
                                      "title": "Standup", "slug": slug})
    assert r.status_code == 200, r.text
    return r.json()["room"]


def test_roundtrip_untrusted_fqid_rejected(server, tmp_path, monkeypatch):
    """Negative: an unpinned/untrusted fqid is rejected by the live server (403).

    No federation key is pinned and no trust grant exists, so verify_signed's
    pubkey resolution fails -> the server returns 403 -> ConfAuthDenied. This is
    the real server rejection path, not a mock.
    """
    tc, post = server
    room = _create_conf(tc)
    # point keystore + trust at empty tmp dirs (no pins, no grants)
    monkeypatch.setattr(
        "skchat.spaces.federation.keystore._DEFAULT_BASE", tmp_path / "no-peers"
    )
    monkeypatch.setattr(
        "skchat.spaces.federation.trust._DEFAULT_PATH", tmp_path / "no-trust.json"
    )
    with pytest.raises(ConfAuthDenied):
        mint_remote_conf_token(
            "http://server", room, fqid="evil@attacker.realm",
            post=post, sign=lambda p: "BOGUS-SIG",
        )


def test_roundtrip_trusted_fqid_mints_token(server, tmp_path, monkeypatch):
    """Positive: a pinned + trusted fqid mints a real conf token end-to-end.

    We pin a verify key (a stub crypto backend accepts the matching sig) and
    grant the fqid full access, then drive the live route. The returned token is
    a real LiveKit JWT signed with the server's secret.
    """
    import json

    tc, post = server
    room = _create_conf(tc)
    fqid = "jarvis@chef.skworld"

    # 1) pin a "pubkey" for the fqid (content is opaque to our stub verifier)
    peers = tmp_path / "peers"
    peers.mkdir()
    (peers / f"{fqid}.asc").write_text("PINNED-PUBKEY")
    monkeypatch.setattr("skchat.spaces.federation.keystore._DEFAULT_BASE", peers)

    # 2) grant the fqid full access via trust policy
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"full_access": [fqid], "default": "deny"}))
    monkeypatch.setattr("skchat.spaces.federation.trust._DEFAULT_PATH", trust)

    # 3) stub the capauth verify backend so the pinned key "verifies" the sig.
    #    (build_signed/verify_signed import the backend lazily.)
    class _StubBackend:
        def verify(self, payload, sig, pub):
            return sig == "GOOD-SIG" and pub == "PINNED-PUBKEY"

    monkeypatch.setattr("capauth.crypto.get_backend", lambda: _StubBackend())

    out = mint_remote_conf_token(
        "http://server", room, fqid=fqid, post=post, sign=lambda p: "GOOD-SIG",
    )
    assert out["identity"] == fqid
    assert out["room"] == room
    assert out["role"] in ("participant", "guest-conf")
    # the token is a genuine LiveKit JWT signed with the server's secret
    decoded = jwt.decode(out["token"], _SECRET, algorithms=["HS256"],
                         options={"verify_aud": False})
    assert decoded["sub"] == fqid
