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
        return Assertion(
            fqid=fqid, space_id=signed_space(signed), issued_at=int(time.time()), nonce="n"
        )

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
    fixed = Assertion(
        fqid="lumina@chef.skworld",
        space_id="space-x",
        issued_at=int(time.time()),
        nonce="replay-nonce",
    )

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


# ── QA Area 3: additional adversarial authd cases ────────────────────────────


def test_verify_failure_aborts_before_minting():
    # If the assertion verification raises (forged sig), authorize must propagate
    # and never reach the mint seam.
    from skchat.spaces.federation.assertion import (
        AssertionError as FedAssertionError,
    )
    from skchat.spaces.federation.nonce import NonceCache

    minted = []

    def _bad_verify(signed, **kw):
        raise FedAssertionError("signature verification failed")

    with pytest.raises(FedAssertionError):
        authorize(
            _signed("lumina@chef.skworld", "space-x"),
            sfu_ws_url="wss://h",
            _verify=_bad_verify,
            _access=lambda f: AccessLevel.FULL,
            _mint=lambda *a: minted.append(a) or "TOKEN",
            _nonce=NonceCache(),
        )
    assert minted == []  # mint was never reached


def test_replay_checked_before_space_and_access():
    # A replayed nonce must be rejected even if the space is live and access is
    # FULL — the nonce check happens first, before any token is minted.
    from skchat.spaces.federation.nonce import NonceCache

    nc = NonceCache()
    fixed = Assertion(
        fqid="lumina@chef.skworld", space_id="space-x", issued_at=int(time.time()), nonce="rn"
    )
    kwargs = dict(
        sfu_ws_url="wss://h",
        _verify=lambda s, **k: fixed,
        _access=lambda f: AccessLevel.FULL,
        _space_live=lambda sid: True,
        _mint=lambda i, r, s: "TOKEN",
        _nonce=nc,
    )
    authorize(_signed("lumina@chef.skworld", "space-x"), **kwargs)  # first ok
    with pytest.raises(AuthDenied, match="replay"):
        authorize(_signed("lumina@chef.skworld", "space-x"), **kwargs)


def test_denied_access_does_not_consume_nonce_slot_for_other_fqid():
    # A DENY for one fqid must not poison the nonce cache for a different fqid
    # reusing the same nonce string (distinct keys).
    from skchat.spaces.federation.nonce import NonceCache

    nc = NonceCache()
    denied = Assertion(
        fqid="ghost@nowhere", space_id="space-x", issued_at=int(time.time()), nonce="shared"
    )
    allowed = Assertion(
        fqid="lumina@chef.skworld", space_id="space-x", issued_at=int(time.time()), nonce="shared"
    )
    with pytest.raises(AuthDenied):
        authorize(
            _signed("ghost@nowhere", "space-x"),
            sfu_ws_url="wss://h",
            _verify=lambda s, **k: denied,
            _access=lambda f: AccessLevel.DENY,
            _mint=lambda *a: "X",
            _nonce=nc,
        )
    # the trusted fqid (different key) still works with the same nonce string
    out = authorize(
        _signed("lumina@chef.skworld", "space-x"),
        sfu_ws_url="wss://h",
        _verify=lambda s, **k: allowed,
        _access=lambda f: AccessLevel.FULL,
        _mint=lambda i, r, s: "TOKEN",
        _nonce=nc,
    )
    assert out["role"] == "speaker"


def test_remote_max_role_listener_does_not_affect_subscribe():
    # Capping remotes at listener only downgrades FULL→listener; a SUBSCRIBE
    # peer is already a listener and must be unaffected (no crash, stays listener).
    from skchat.spaces.federation.nonce import NonceCache

    out = authorize(
        _signed("rando@other", "space-x"),
        sfu_ws_url="wss://h",
        _verify=_verify_to("rando@other"),
        _access=lambda f: AccessLevel.SUBSCRIBE,
        _remote_max_role="listener",
        _mint=lambda i, r, s: f"TOKEN:{r.value}",
        _nonce=NonceCache(),
    )
    assert out["role"] == "listener"


def test_identity_and_space_echoed_in_response():
    from skchat.spaces.federation.nonce import NonceCache

    out = authorize(
        _signed("lumina@chef.skworld", "space-x"),
        sfu_ws_url="wss://h:8443",
        _verify=_verify_to("lumina@chef.skworld"),
        _access=lambda f: AccessLevel.FULL,
        _mint=lambda i, r, s: "T",
        _nonce=NonceCache(),
    )
    assert out["identity"] == "lumina@chef.skworld"
    assert out["space_id"] == "space-x"
    assert out["sfu_ws_url"] == "wss://h:8443"
