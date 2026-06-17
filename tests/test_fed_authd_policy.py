import time

import pytest

from skchat.spaces.federation.assertion import Assertion, build_signed
from skchat.spaces.federation.authd import AuthDenied, authorize
from skchat.spaces.federation.nonce import NonceCache
from skchat.spaces.federation.trust import AccessLevel


def _signed(fqid, space):
    a = Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()), nonce="n")
    return build_signed(a, sign=lambda p: "SIG")


def _verify_to(fqid, space):
    def _v(signed, **kw):
        return Assertion(fqid=fqid, space_id=space, issued_at=int(time.time()), nonce="n")

    return _v


def _mint(identity, role, space):
    return f"TOKEN:{role.value}"


def test_full_access_capped_to_listener_when_configured():
    out = authorize(
        _signed("opus@chef.skworld", "space-x"),
        sfu_ws_url="wss://h",
        _verify=_verify_to("opus@chef.skworld", "space-x"),
        _access=lambda f: AccessLevel.FULL,
        _remote_max_role="listener",  # operator caps remotes at listener
        _mint=_mint,
        _nonce=NonceCache(),
    )
    assert out["role"] == "listener"


def test_full_access_is_speaker_by_default():
    out = authorize(
        _signed("opus@chef.skworld", "space-x"),
        sfu_ws_url="wss://h",
        _verify=_verify_to("opus@chef.skworld", "space-x"),
        _access=lambda f: AccessLevel.FULL,
        _mint=_mint,
        _nonce=NonceCache(),
    )
    assert out["role"] == "speaker"


def test_unknown_space_is_denied():
    with pytest.raises(AuthDenied, match="space"):
        authorize(
            _signed("opus@chef.skworld", "space-gone"),
            sfu_ws_url="wss://h",
            _verify=_verify_to("opus@chef.skworld", "space-gone"),
            _access=lambda f: AccessLevel.FULL,
            _space_live=lambda sid: False,  # space doesn't exist / not live
            _mint=_mint,
            _nonce=NonceCache(),
        )


def test_live_space_passes():
    out = authorize(
        _signed("opus@chef.skworld", "space-x"),
        sfu_ws_url="wss://h",
        _verify=_verify_to("opus@chef.skworld", "space-x"),
        _access=lambda f: AccessLevel.FULL,
        _space_live=lambda sid: True,
        _mint=_mint,
        _nonce=NonceCache(),
    )
    assert out["role"] == "speaker"
