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


# ── QA Area 3: additional adversarial cases ──────────────────────────────────


def test_tampered_claim_body_rejected():
    # An attacker keeps a VALID signature but swaps the claim body (e.g. to
    # escalate the space or change the fqid). The sig no longer matches the new
    # claim bytes, so verification must fail. This is the core forgery defence.
    a = Assertion(fqid="lumina@chef.skworld", space_id="space-x",
                  issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    tampered = dict(json.loads(signed["claim"]))
    tampered["space_id"] = "admin-space"          # escalation attempt
    signed["claim"] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    with pytest.raises(FedAssertionError, match="signature"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_fqid_swap_with_kept_signature_rejected():
    # Swap only the fqid in the claim while keeping the original signature — the
    # signature was computed over the OLD fqid, so it must not verify.
    a = Assertion(fqid="rando@other.realm", space_id="space-x",
                  issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    swapped = dict(json.loads(signed["claim"]))
    swapped["fqid"] = "lumina@chef.skworld"       # impersonate a trusted peer
    signed["claim"] = json.dumps(swapped, sort_keys=True, separators=(",", ":"))
    with pytest.raises(FedAssertionError, match="signature"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_max_age_zero_disables_freshness_window():
    # max_age=0 is the documented "no freshness check" sentinel — an ancient
    # assertion is accepted (signature still required). Guards against an
    # accidental flip where 0 would reject everything.
    a = Assertion(fqid="x@y.z", space_id="space-x",
                  issued_at=int(time.time()) - 999999, nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    out = verify_signed(signed, resolve_pubkey=lambda f: "PUB",
                        verify=_fake_verify_ok, max_age=0)
    assert out.fqid == "x@y.z"


@pytest.mark.parametrize("missing", ["fqid", "space_id", "issued_at", "nonce"])
def test_claim_missing_required_field_rejected(missing):
    a = Assertion(fqid="x@y.z", space_id="s", issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    d = json.loads(signed["claim"])
    del d[missing]
    signed["claim"] = json.dumps(d)
    with pytest.raises(FedAssertionError, match="malformed claim"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_non_json_claim_rejected():
    with pytest.raises(FedAssertionError, match="malformed claim"):
        verify_signed({"claim": "not-json{", "sig": "x"},
                      resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_non_object_json_claim_rejected():
    # a JSON array / scalar parses but has no fqid → malformed
    with pytest.raises(FedAssertionError, match="malformed claim"):
        verify_signed({"claim": "[1,2,3]", "sig": "x"},
                      resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_non_integer_issued_at_rejected():
    a = Assertion(fqid="x@y.z", space_id="s", issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)
    d = json.loads(signed["claim"])
    d["issued_at"] = "not-a-number"
    signed["claim"] = json.dumps(d)
    with pytest.raises(FedAssertionError, match="malformed claim"):
        verify_signed(signed, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_empty_signed_dict_rejected():
    # no claim, no sig → empty claim string → malformed
    with pytest.raises(FedAssertionError):
        verify_signed({}, resolve_pubkey=lambda f: "PUB", verify=_fake_verify_ok)


def test_resolve_pubkey_is_called_with_full_fqid():
    # The resolver MUST receive the realm-qualified fqid (not the bare agent) —
    # otherwise lumina@chef.skworld and lumina@evil.attacker collide (S5 C1).
    seen = []
    a = Assertion(fqid="lumina@chef.skworld", space_id="s",
                  issued_at=int(time.time()), nonce="n")
    signed = build_signed(a, sign=_fake_sign)

    def _resolver(fqid):
        seen.append(fqid)
        return "PUB"

    verify_signed(signed, resolve_pubkey=_resolver, verify=_fake_verify_ok)
    assert seen == ["lumina@chef.skworld"]
