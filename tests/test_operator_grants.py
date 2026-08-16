"""The enrolled operator device must be granted BOTH skchat.prekey and the
least-sensitive skchat.inbox capability, non-expiring, so the authz PDP agrees
with the legitimate legacy allow for the operator seat's own inbox polling
(CR-3.1: closes the 227k-hit shadow divergence that blocked the enforce flip).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    # capauth default_base_dir() == Path.home()/.skcapstone; pin home to tmp.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    base = tmp_path / ".skcapstone"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _caps_for(home_dir, subject):
    from capauth.tokens import list_tokens

    caps = set()
    for t in list_tokens(home_dir):
        subj = getattr(t.payload, "subject", None)
        if subj == subject and t.payload.is_active:
            caps.update(getattr(t.payload, "capabilities", []) or [])
    return caps


def test_grant_mints_prekey_and_inbox_nonexpiring(home):
    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import grant_operator_capabilities

    from .conftest import operator_device_and_proof

    # inc-c72a9120 part 2: mode="verified" now requires a real device-signed
    # proof (capauth card N10 + the ECDSA key-shape widening in capauth's
    # fix/device-key-proof). A real cryptography P-256 keypair and a real
    # signature -- no mocked verifier.
    device_fp, pubkey_b64, proof = operator_device_and_proof()

    ok = grant_operator_capabilities(device_fp, pubkey_b64, proof=proof)
    assert ok is True

    subject = operator_subject(device_fp)
    caps = _caps_for(home, subject)
    assert "skchat.prekey" in caps, caps
    assert "skchat.inbox" in caps, caps

    # non-expiring: no active token for the subject carries an expiry
    from capauth.tokens import list_tokens

    for t in list_tokens(home):
        if getattr(t.payload, "subject", None) == subject:
            assert t.payload.expires_at is None, "operator grant must not expire"


def test_grant_refuses_and_logs_when_no_proof_is_presented(home, caplog):
    """inc-c72a9120: the original bug. An older client that presents no
    device-signed proof must NOT silently land with zero capabilities (that
    was the whole incident) and must NOT be quietly downgraded to a weaker
    enrollment mode either (skchat.send/groups/calls are min-mode VERIFIED, so
    a `tofu` device-record would just make `decide()` deny those later, for a
    reason that looks unrelated to "no proof was presented"). The grant is
    refused outright, before ever calling capauth, and logged at ERROR (not
    WARNING -- WARNING is the level that let this go unnoticed) naming the
    subject, which embeds device_fp.
    """
    import logging

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import grant_operator_capabilities

    device_fp = "deadbeefcafe0001"
    pubkey_b64 = "TESTPUBKEYb64AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    subject = operator_subject(device_fp)

    with caplog.at_level(logging.ERROR, logger="skchat.operator_grants"):
        ok = grant_operator_capabilities(device_fp, pubkey_b64, proof=None)

    assert ok is False
    assert _caps_for(home, subject) == set()
    assert any(
        record.levelno >= logging.ERROR and subject in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_grant_refuses_proof_signed_over_the_raw_operator_subject(home):
    """Pins the inc-c72a9120 canonicalisation trap directly (see
    ``operator_device_and_proof``'s docstring for the full story): a proof
    signed over the RAW ``operator:<fp>`` bytes -- what a naive client
    implementation would produce -- is a real signature made by the right key,
    just over the WRONG bytes, and capauth must refuse it exactly like a
    missing proof, never accept it.
    """
    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import grant_operator_capabilities

    from .conftest import operator_device_and_proof

    device_fp, pubkey_b64, wrong_proof = operator_device_and_proof(canonicalize=False)

    ok = grant_operator_capabilities(device_fp, pubkey_b64, proof=wrong_proof)

    assert ok is False
    assert _caps_for(home, operator_subject(device_fp)) == set()


def test_grant_refuses_a_proof_from_a_different_device(home):
    """A real signature, over the right kind of bytes, just made by a
    DIFFERENT device's key than the one being enrolled -- proves possession
    of someone else's key, not this one's."""
    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import grant_operator_capabilities

    from .conftest import operator_device_and_proof

    device_fp, pubkey_b64, _own_proof = operator_device_and_proof()
    _other_fp, _other_pub, other_proof = operator_device_and_proof()

    ok = grant_operator_capabilities(device_fp, pubkey_b64, proof=other_proof)

    assert ok is False
    assert _caps_for(home, operator_subject(device_fp)) == set()


def test_backfill_adds_inbox_to_existing_prekey_only_device(home):
    """A device previously granted prekey-only gets inbox added by the backfill."""
    from capauth.tokens import issue_token

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_grants import backfill_operator_capabilities

    subject = operator_subject("f00dfeed12345678")
    # simulate the old-world state: a prekey-only grant
    issue_token(home, subject, ["skchat.prekey"], sign=False)
    assert "skchat.inbox" not in _caps_for(home, subject)

    n = backfill_operator_capabilities(base_dir=home)
    assert n >= 1
    assert "skchat.inbox" in _caps_for(home, subject)
