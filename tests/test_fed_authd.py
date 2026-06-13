import time

import pytest

from skchat.spaces.federation.assertion import Assertion, build_signed
from skchat.spaces.federation.authd import AuthDenied, authorize
from skchat.spaces.federation.trust import AccessLevel


def _signed(fqid, space):
    a = Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()), nonce="n")
    return build_signed(a, sign=lambda p: "SIG")


def _verify_to(fqid):
    # inject a verifier that always returns this fqid as verified
    def _v(signed, **kw):
        return Assertion(fqid=fqid, space_id=signed_space(signed),
                         issued_at=int(time.time()), nonce="n")
    return _v


def signed_space(signed):
    import json
    return json.loads(signed["claim"])["space_id"]


def test_full_access_gets_host_token():
    out = authorize(
        _signed("lumina@chef.skworld", "space-x"),
        sfu_ws_url="wss://h:8443",
        _verify=_verify_to("lumina@chef.skworld"),
        _access=lambda f: AccessLevel.FULL,
        _mint=lambda identity, role, space: f"TOKEN:{role}:{space}",
    )
    assert out["sfu_ws_url"] == "wss://h:8443"
    assert out["role"] == "speaker"
    assert out["token"].startswith("TOKEN:")


def test_subscribe_access_gets_listener_token():
    out = authorize(
        _signed("rando@other", "space-x"),
        sfu_ws_url="wss://h:8443",
        _verify=_verify_to("rando@other"),
        _access=lambda f: AccessLevel.SUBSCRIBE,
        _mint=lambda identity, role, space: f"TOKEN:{role}",
    )
    assert out["role"] == "listener"


def test_denied_access_raises():
    with pytest.raises(AuthDenied):
        authorize(
            _signed("ghost@nowhere", "space-x"),
            sfu_ws_url="wss://h:8443",
            _verify=_verify_to("ghost@nowhere"),
            _access=lambda f: AccessLevel.DENY,
            _mint=lambda *a: "X",
        )


def test_replay_same_nonce_is_rejected():
    from skchat.spaces.federation.nonce import NonceCache

    nc = NonceCache()
    # a verifier that always returns the SAME fqid+nonce (a replayed assertion)
    fixed = Assertion(fqid="lumina@chef.skworld", space_id="space-x",
                      issued_at=int(time.time()), nonce="replay-nonce")

    def _v(signed, **kw):
        return fixed

    kwargs = dict(
        sfu_ws_url="wss://h:8443",
        _verify=_v,
        _access=lambda f: AccessLevel.FULL,
        _mint=lambda identity, role, space: "TOKEN",
        _nonce=nc,
    )
    out = authorize(_signed("lumina@chef.skworld", "space-x"), **kwargs)
    assert out["token"] == "TOKEN"
    with pytest.raises(AuthDenied, match="replay"):
        authorize(_signed("lumina@chef.skworld", "space-x"), **kwargs)


def test_sfu_get_route_rejects_malformed_body():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skchat.spaces.routes import register_spaces_routes

    app = FastAPI()
    register_spaces_routes(app)
    client = TestClient(app)
    # not valid JSON => 400
    resp = client.post("/sfu/get", content=b"not json")
    assert resp.status_code == 400
