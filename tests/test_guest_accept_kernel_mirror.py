"""M2 guest-store fold: durable admissions mirror into capauth.pairing, fail-safe.

A durable ConsumedNonces store (real db path) dual-writes every admission, trust,
and revocation into capauth.pairing. In-memory stores never mirror. The mirror is
best-effort: a capauth error can never break the SQLite admission.

The pins here are REAL shapes on purpose. These tests used to pass placeholders
like ``peerfp1`` and ``op2@host``, which no production path can produce
(``guest_group_routes`` derives both from ``pubkey_fingerprint``, and an
operator id is an fqid). capauth's canonical-subject work then began refusing
them, and the suite went red on fixtures rather than on behaviour. A fixture
that cannot occur in production tests nothing about production.
"""

from __future__ import annotations

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

from skchat.guest_accept import ConsumedNonces

pytestmark = pytest.mark.usefixtures("stub_token_signing")

#: Real shapes: a 40-hex PGP fingerprint (what pubkey_fingerprint returns) and a
#: canonical operator fqid. capauth stores a bare fingerprint under the
#: ``device:`` seat prefix, which is why the lookups below are not the pin.
PEER_FP = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PEER_FP_2 = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
PEER_SUBJECT = f"device:{PEER_FP}"
PEER_SUBJECT_2 = f"device:{PEER_FP_2}"
OPERATOR = "op@chef.skworld.io"


@pytest.fixture
def durable_store(tmp_path, monkeypatch):
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)  # default ON
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", str(tmp_path / "capauth"))
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    yield store, str(tmp_path / "capauth")
    store.close()


def _devices(subject, base):
    from capauth.pairing import list_devices

    return list_devices(subject=subject, base_dir=base)


def test_admission_mirrors_tofu_device(durable_store):
    store, base = durable_store
    store.record_admission(PEER_FP, OPERATOR, "{}", "sigA", "sigB")
    devs = _devices(PEER_SUBJECT, base)
    assert len(devs) == 1
    assert devs[0].mode.value == "tofu"
    assert devs[0].subject == PEER_SUBJECT
    assert not devs[0].revoked


def test_admission_is_stored_canonically_and_found_by_either_spelling(durable_store):
    """Enrolled under ``device:<fp>``, findable by the bare pin as well.

    The second half is capauth's guarantee, not skchat's: ``list_devices``
    matches a filter against the raw spelling AND its canonical form (capauth
    card N3). skchat depends on it, so it is asserted here rather than assumed,
    because it is exactly what lets :func:`mirror_revocation` keep passing the
    raw pin.
    """
    store, base = durable_store
    store.record_admission(PEER_FP, OPERATOR, "{}", "sigA", "sigB")
    assert _devices(PEER_SUBJECT, base)[0].subject == PEER_SUBJECT
    assert [d.device_id for d in _devices(PEER_FP, base)] == [
        d.device_id for d in _devices(PEER_SUBJECT, base)
    ]


def test_trust_operator_mirrors_tofu_not_attested(durable_store):
    """Mode B records tofu, because no attestation exists on this path.

    skchat holds only the remote operator's PUBLIC key. ``attested`` asserts
    that the vouching operator signed capauth's attested challenge, and since
    capauth 0.3.0 that signature is actually checked. Asking for ``attested``
    here used to be a silent no-op: SQLite recorded the trust and capauth
    recorded nothing.
    """
    store, base = durable_store
    store.trust_operator(OPERATOR, "PUBKEY-ARMOR")
    devs = _devices(OPERATOR, base)
    assert len(devs) == 1
    assert devs[0].mode.value == "tofu"


def test_revocation_mirrors_revoke(durable_store):
    store, base = durable_store
    store.record_admission(PEER_FP_2, OPERATOR, "{}", "s1", "s2")
    assert not _devices(PEER_SUBJECT_2, base)[0].revoked
    store.revoke_pin(PEER_FP_2)
    assert all(d.revoked for d in _devices(PEER_SUBJECT_2, base))


def test_revoking_by_bare_pin_revokes_the_canonical_device(durable_store):
    """The cross-spelling case, end to end, and the one that can silently rot.

    Admission enrolls ``device:<fp>``; ``revoke_pin`` is called with the bare
    ``<fp>`` (``GuestTrustStore.revoke_pin`` takes a peer_fp or an operator
    id). If those two ever stop resolving to the same record, the revoke
    matches nothing, returns cleanly, and leaves a live credential behind with
    nothing anywhere reporting a problem.
    """
    store, base = durable_store
    store.record_admission(PEER_FP, OPERATOR, "{}", "s1", "s2")
    assert not _devices(PEER_SUBJECT, base)[0].revoked
    store.revoke_pin(PEER_FP)
    devs = _devices(PEER_SUBJECT, base)
    assert devs and all(d.revoked for d in devs)


def test_in_memory_store_never_mirrors(tmp_path, monkeypatch):
    # The existing guest tests use :memory: and must produce zero capauth writes.
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    store = ConsumedNonces(db_path=":memory:")
    store.record_admission(PEER_FP, OPERATOR, "{}", "s1", "s2")
    store.trust_operator(OPERATOR, "PUB")
    assert _devices(PEER_SUBJECT, base) == []
    assert _devices(OPERATOR, base) == []


def test_mirror_failure_never_breaks_admission(tmp_path, monkeypatch):
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", str(tmp_path / "capauth"))

    def _boom(*a, **k):
        raise RuntimeError("capauth down")

    monkeypatch.setattr("capauth.pairing.enroll_device", _boom)
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    # The admission must still succeed in SQLite despite the mirror blowing up.
    store.record_admission(PEER_FP, OPERATOR, "{}", "s1", "s2")
    assert store.is_admitted(PEER_FP) is True
    store.close()


def test_uncanonicalizable_pin_never_breaks_admission(tmp_path, monkeypatch):
    """A pin capauth refuses is logged and swallowed, not raised at the caller.

    Guards the new canonicalization step against becoming a new way for the
    mirror to break live guest admission, which is the invariant this whole
    module exists to hold.
    """
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", str(tmp_path / "capauth"))
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    store.record_admission("not-a-valid-subject", OPERATOR, "{}", "s1", "s2")
    assert store.is_admitted("not-a-valid-subject") is True
    store.close()


def test_kernel_disabled_skips_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL", "0")
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    store.record_admission(PEER_FP, OPERATOR, "{}", "s1", "s2")
    assert _devices(PEER_SUBJECT, base) == []  # kernel off -> no mirror
    store.close()
