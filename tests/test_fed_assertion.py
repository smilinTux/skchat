import json
import time

import pytest

from skchat.spaces.federation.assertion import (
    Assertion,
    build_signed,
    verify_signed,
)
from skchat.spaces.federation.assertion import (
    AssertionError as FedAssertionError,
)


def _fake_sign(payload: bytes) -> str:
    # deterministic stand-in for capauth PGP signing
    return "SIG(" + payload.decode() + ")"


def _fake_verify_ok(payload: bytes, sig: str, pub: str) -> bool:
    return sig == "SIG(" + payload.decode() + ")"


def test_build_and_verify_roundtrip():
    a = Assertion(fqid="lumina@chef.skworld", space_id="space-x",
                  issued_at=int(time.time()), nonce="abc")
    signed = build_signed(a, sign=_fake_sign)
    assert signed["sig"]
    out = verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)
    assert out.fqid == "lumina@chef.skworld"
    assert out.space_id == "space-x"


def test_verify_rejects_bad_signature():
    a = Assertion(fqid="x@y.z", space_id="space-x", issued_at=int(time.time()),
                  nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    signed["sig"] = "SIG(tampered)"
    with pytest.raises(FedAssertionError, match="signature"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_verify_rejects_unknown_signer():
    a = Assertion(fqid="ghost@nowhere", space_id="space-x",
                  issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    with pytest.raises(FedAssertionError, match="pubkey"):
        verify_signed(signed, resolve_pubkey=lambda f: None, verify=_fake_verify_ok)


def test_verify_rejects_stale_assertion():
    a = Assertion(fqid="x@y.z", space_id="space-x",
                  issued_at=int(time.time()) - 9999, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    with pytest.raises(FedAssertionError, match="expired|stale"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB",
                      verify=_fake_verify_ok, max_age=300)


def test_future_dated_assertion_rejected():
    # an assertion claiming to be issued far in the future is also stale/invalid
    a = Assertion(fqid="x@y.z", space_id="space-x",
                  issued_at=int(time.time()) + 9999, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    with pytest.raises(FedAssertionError, match="expired|stale|future"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB",
                      verify=_fake_verify_ok, max_age=300)


def test_small_future_skew_is_tolerated():
    a = Assertion(fqid="x@y.z", space_id="space-x",
                  issued_at=int(time.time()) + 30, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    out = verify_signed(signed, resolve_pubkey=lambda f: "PUB",
                        verify=_fake_verify_ok, max_age=300)
    assert out.fqid == "x@y.z"


@pytest.mark.parametrize("bad", ["@chef.skworld", "chef.skworld", "a@b@c", "a@", ""])
def test_malformed_fqid_rejected(bad):
    a = Assertion(fqid=bad, space_id="space-x", issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    with pytest.raises(FedAssertionError, match="malformed"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_signed_payload_is_canonical_json():
    a = Assertion(fqid="x@y.z", space_id="s", issued_at=10, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    # the signed bytes are the canonical (sorted-keys) JSON of the claim
    claim = json.loads(signed["claim"])
    assert claim == {"fqid": "x@y.z", "space_id": "s", "issued_at": 10, "nonce": "n"}
