"""capauth card N10: a ``verified`` enrollment must carry a REAL proof.

capauth 0.3.0 made the enrollment mode's evidence mandatory rather than
caller-asserted. ``enroll_device(..., mode="verified")`` with no ``proof`` now
raises :class:`capauth.pairing.kernel.PairingError`, and skchat's operator grant
caught that exception and returned ``False``. The visible symptom was not an
error: the enrollment response still said 200, but NOTHING was enrolled and no
token was issued, so every later ``decide()`` denied the device for "no enrolled
device" with nothing pointing at the real cause. Only a process RESTART exposed
it, because a running service keeps executing the pre-N10 capauth it imported at
start.

The test that would have caught this asserts the grant actually ENROLLS and
issues a usable token, never merely that it returned False or did not raise. So
that is what this suite asserts, in both directions:

  * with a real proof, the device record lands at ``verified`` and the PDP
    ALLOWS the verified-tier ``skchat.send``;
  * with no proof, the device is still enrolled (at the honest ``tofu`` floor,
    which is a strict improvement on enrolling nothing) but is NEVER recorded as
    ``verified``, and the PDP denies the tiers that were not proven.

Claiming a tier you did not prove is precisely what N10 exists to stop:
``verified`` gates ``skcode.dispatch``, which is remote code execution as the
subject.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest

# capauth signs capability tokens with gpg, and a signing failure RAISES rather
# than quietly minting an unsigned token. capauth.testing is capauth's own
# shipped seam: it fakes the gpg SUBPROCESS boundary only, so tokens are really
# signed and really verified in-process. Applied per module, never
# directory-wide, so the suites that sign with a real ephemeral gpg key keep
# exercising the real path.
from capauth.testing import stub_token_signing  # noqa: F401
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

pytestmark = pytest.mark.usefixtures("stub_token_signing")


def _kp() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """A WebCrypto-shaped device key: ECDSA P-256, base64 DER SPKI public half."""
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, base64.b64encode(spki).decode()


def _sign_p1363(priv, payload: bytes) -> str:
    """Sign as WebCrypto does: base64 of the 64-byte P1363 ``r||s``."""
    der = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def home(tmp_path, monkeypatch):
    # capauth's default_base_dir() is Path.home()/.skcapstone; pin home to tmp so
    # the grant writes and decide() reads the same hermetic store.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    base = tmp_path / ".skcapstone"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _device_mode(home_dir, subject) -> str | None:
    from capauth.pairing import list_devices

    for dev in list_devices(subject, base_dir=home_dir):
        mode = getattr(dev, "mode", None)
        return getattr(mode, "value", mode)
    return None


def _caps_for(home_dir, subject) -> set[str]:
    from capauth.tokens import list_tokens

    caps: set[str] = set()
    for t in list_tokens(home_dir):
        if getattr(t.payload, "subject", None) == subject and t.payload.is_active:
            caps.update(getattr(t.payload, "capabilities", []) or [])
    return caps


# ── the challenge helper ────────────────────────────────────────────────────


def test_challenge_is_the_exact_bytes_capauth_accepts(home):
    """Round-trip guard against capauth drift.

    ``verified_enrollment_challenge`` re-derives what ``enroll_device`` computes
    internally: capauth's OWN fingerprint of the presented key (a 40-char
    uppercase hex, NOT skchat's 16-char ``device_fingerprint``) and the
    CANONICALIZED subject (``operator:<fp>`` normalizes to ``device:<fp>``). Both
    corrections matter: signing over skchat's fingerprint, or over the raw
    ``operator:`` subject, produces a signature capauth rejects. If capauth ever
    changes either derivation this test fails loudly instead of the grant
    silently sliding back to the tofu floor in production.
    """
    from capauth.pairing import enroll_device

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import verified_enrollment_challenge

    priv, pub = _kp()
    subject = operator_subject(device_fingerprint(pub))
    challenge = verified_enrollment_challenge(pub, subject=subject)

    # Domain-separated, and bound to BOTH the key and the identity claimed.
    assert challenge.startswith(b"capauth-pairing-enrollment-verified-v1:")

    enrollment = enroll_device(
        pub,
        ["skchat.inbox"],
        mode="verified",
        subject=subject,
        base_dir=home,
        proof=_sign_p1363(priv, challenge),
    )
    assert enrollment.mode.value == "verified"


def test_challenge_cannot_be_replayed_as_an_attestation(home):
    """The verified proof must not satisfy the attested check, and vice versa."""
    from capauth.pairing import attested_challenge
    from capauth.pairing.kernel import PairingError, enroll_device
    from capauth.pairing.store import fingerprint_for

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import verified_enrollment_challenge

    priv, pub = _kp()
    subject = operator_subject(device_fingerprint(pub))
    verified_proof = _sign_p1363(priv, verified_enrollment_challenge(pub, subject=subject))

    with pytest.raises(PairingError):
        enroll_device(
            pub,
            ["skchat.inbox"],
            mode="attested",
            subject=subject,
            base_dir=home,
            operator_pubkey=pub,
            attestation=verified_proof,
        )

    # ...and the attested signature is not accepted as the verified self-proof.
    from capauth.subject import canonical_subject

    attested_sig = _sign_p1363(
        priv, attested_challenge(fingerprint_for(pub), canonical_subject(subject))
    )
    with pytest.raises(PairingError):
        enroll_device(
            pub,
            ["skchat.inbox"],
            mode="verified",
            subject=subject,
            base_dir=home,
            proof=attested_sig,
        )


# ── the grant ───────────────────────────────────────────────────────────────


def test_grant_with_real_proof_enrolls_verified_and_the_pdp_allows_send(home):
    """The whole point: a proof-bearing grant ENROLLS and the token WORKS.

    Asserts the outcome (a verified device record, an active token, and a real
    ``decide()`` ALLOW on the verified-tier capability), never that the call
    merely returned truthy or did not raise.
    """
    from capauth.authz import decide

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import (
        SEND_CAPABILITY,
        grant_operator_capabilities,
        verified_enrollment_challenge,
    )

    priv, pub = _kp()
    device_fp = device_fingerprint(pub)
    subject = operator_subject(device_fp)
    proof = _sign_p1363(priv, verified_enrollment_challenge(pub, subject=subject))

    assert grant_operator_capabilities(device_fp, pub, capauth_proof=proof) is True

    assert _device_mode(home, subject) == "verified"
    assert SEND_CAPABILITY in _caps_for(home, subject)
    assert decide(subject, SEND_CAPABILITY, base_dir=home).allow is True


def test_grant_without_proof_enrolls_tofu_and_never_claims_verified(home, caplog):
    """No proof available: enroll at the tier that IS provable, and say so.

    A device is still enrolled, which is a strict improvement on the broken
    state where the PairingError was swallowed and NOTHING was recorded, so the
    least-sensitive capability keeps working. But the record must not say
    ``verified``, and the verified-tier capability must be denied by the real
    PDP rather than granted on an unproven claim.
    """
    from capauth.authz import decide

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import (
        INBOX_CAPABILITY,
        SEND_CAPABILITY,
        grant_operator_capabilities,
    )

    _priv, pub = _kp()
    device_fp = device_fingerprint(pub)
    subject = operator_subject(device_fp)

    with caplog.at_level(logging.WARNING, logger="skchat.operator_grants"):
        assert grant_operator_capabilities(device_fp, pub) is True

    # Enrolled, at the honest floor.
    assert _device_mode(home, subject) == "tofu"
    assert decide(subject, INBOX_CAPABILITY, base_dir=home).allow is True
    # Not the tier it could not prove.
    assert decide(subject, SEND_CAPABILITY, base_dir=home).allow is False

    # And it is LOUD: silence is why this shipped unnoticed.
    downgrades = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert downgrades, "a tier downgrade must be logged at WARNING or above"
    blob = " ".join(r.getMessage() for r in downgrades)
    assert "tofu" in blob and "verified" in blob
    assert SEND_CAPABILITY in blob, "the log must name the capabilities now denied"


def test_grant_with_a_forged_proof_does_not_reach_verified(home, caplog):
    """A signature over the WRONG bytes must not buy the verified tier.

    Negative control for the test above: if a bad proof were accepted, the
    passing "with proof" test would prove nothing.
    """
    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import grant_operator_capabilities

    priv, pub = _kp()
    device_fp = device_fingerprint(pub)
    subject = operator_subject(device_fp)
    forged = _sign_p1363(priv, b"some other bytes entirely")

    with caplog.at_level(logging.WARNING, logger="skchat.operator_grants"):
        grant_operator_capabilities(device_fp, pub, capauth_proof=forged)

    assert _device_mode(home, subject) == "tofu"
    # A proof that was PRESENTED and REJECTED is a stronger signal than an
    # absent one, so it must be louder than a warning.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_grant_with_a_proof_from_a_different_key_does_not_reach_verified(home):
    """Possession of some private key is not possession of THIS device's key."""
    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import grant_operator_capabilities, verified_enrollment_challenge

    _priv, pub = _kp()
    attacker_priv, _attacker_pub = _kp()
    device_fp = device_fingerprint(pub)
    subject = operator_subject(device_fp)

    # Correct challenge bytes, wrong signer.
    proof = _sign_p1363(attacker_priv, verified_enrollment_challenge(pub, subject=subject))
    grant_operator_capabilities(device_fp, pub, capauth_proof=proof)

    assert _device_mode(home, subject) == "tofu"


def test_backfill_never_re_modes_a_device_it_cannot_prove(home, caplog):
    """The backfill holds only PUBLIC keys, so it can never mint a proof.

    It must therefore top the capability TOKENS up (its actual job) and leave
    every enrollment mode alone, rather than either claiming ``verified`` it
    cannot prove or silently DOWNGRADING an already-verified device record to
    tofu. Both would be wrong; the second would be a live regression.
    """
    from capauth.tokens import issue_token

    from skchat.dataplane_auth import operator_subject
    from skchat.operator_auth import device_fingerprint
    from skchat.operator_grants import (
        INBOX_CAPABILITY,
        SEND_CAPABILITY,
        backfill_operator_capabilities,
        grant_operator_capabilities,
        verified_enrollment_challenge,
    )

    priv, pub = _kp()
    device_fp = device_fingerprint(pub)
    subject = operator_subject(device_fp)
    proof = _sign_p1363(priv, verified_enrollment_challenge(pub, subject=subject))
    grant_operator_capabilities(device_fp, pub, capauth_proof=proof)
    assert _device_mode(home, subject) == "verified"

    # Simulate the old-world token state this backfill exists to repair.
    issue_token(home, subject, [INBOX_CAPABILITY], sign=False)

    with caplog.at_level(logging.WARNING, logger="skchat.operator_grants"):
        assert backfill_operator_capabilities(base_dir=home) >= 1

    # Tokens topped up, mode untouched.
    assert SEND_CAPABILITY in _caps_for(home, subject)
    assert _device_mode(home, subject) == "verified"
