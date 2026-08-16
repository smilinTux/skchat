"""``mirror_trusted_operator`` under capauth card N10.

Guest-accept Mode B mirrored the trusted operator at ``mode="attested"`` with an
``operator_pubkey`` but no ``attestation``. Since capauth 0.3.0 that raises, and
the handler was ``logger.debug``, so the mirror became a silent no-op: the SQLite
trust store recorded the operator and capauth recorded nothing at all.

``attested`` means a VOUCHING OPERATOR signed for this device/identity pair. On
this path skchat holds only the remote operator's PUBLIC key (the local operator
chose to trust it; nobody signed capauth's attested challenge), so there is no
attestation to present and none can be manufactured. The honest mode for
"pin a key the operator chose to trust, on first sight" is exactly ``tofu``, so
that is the floor it falls back to, loudly. A caller that genuinely holds an
attestation can pass one and get real ``attested``.
"""

from __future__ import annotations

import base64
import logging

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def _kp():
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, base64.b64encode(spki).decode()


def _sign_p1363(priv, payload: bytes) -> str:
    der = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def kernel_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL", "1")
    base = tmp_path / "kernel-home"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", str(base))
    return base


def _devices(base, subject):
    from capauth.pairing import list_devices

    return list_devices(subject, base_dir=str(base))


def test_trusted_operator_without_attestation_is_still_mirrored_as_tofu(kernel_home, caplog):
    """The mirror must RECORD something, at a mode it can defend, and say so."""
    from skchat.pairing_mirror import mirror_trusted_operator

    _priv, pub = _kp()
    operator_id = "peer-operator-alpha@skworld.io"

    with caplog.at_level(logging.WARNING, logger="skchat.pairing_mirror"):
        mirror_trusted_operator(operator_id, pub)

    devices = _devices(kernel_home, operator_id)
    assert devices, "the mirror recorded nothing: this is the silent no-op being fixed"
    mode = getattr(devices[0].mode, "value", devices[0].mode)
    assert mode == "tofu"

    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned, "a mode the caller asked for and could not prove must be logged"
    blob = " ".join(r.getMessage() for r in warned)
    assert "attested" in blob and "tofu" in blob


def test_trusted_operator_with_a_real_attestation_is_mirrored_as_attested(kernel_home):
    """A caller that genuinely holds an attestation gets the real tier."""
    from capauth.pairing import attested_challenge
    from capauth.pairing.store import fingerprint_for
    from capauth.subject import canonical_subject

    from skchat.pairing_mirror import mirror_trusted_operator

    priv, pub = _kp()
    operator_id = "peer-operator-beta@skworld.io"
    attestation = _sign_p1363(
        priv, attested_challenge(fingerprint_for(pub), canonical_subject(operator_id))
    )

    mirror_trusted_operator(operator_id, pub, attestation=attestation)

    devices = _devices(kernel_home, operator_id)
    assert devices
    assert getattr(devices[0].mode, "value", devices[0].mode) == "attested"


def test_a_bogus_attestation_does_not_buy_the_attested_tier(kernel_home, caplog):
    """Negative control: a signature over the wrong bytes falls back to tofu."""
    from skchat.pairing_mirror import mirror_trusted_operator

    priv, pub = _kp()
    operator_id = "peer-operator-gamma@skworld.io"

    with caplog.at_level(logging.WARNING, logger="skchat.pairing_mirror"):
        mirror_trusted_operator(operator_id, pub, attestation=_sign_p1363(priv, b"wrong bytes"))

    devices = _devices(kernel_home, operator_id)
    assert devices
    assert getattr(devices[0].mode, "value", devices[0].mode) == "tofu"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_admission_mirror_is_unaffected(kernel_home):
    """The TOFU admission path is not gated by N10 and must keep working."""
    from skchat.pairing_mirror import mirror_admission

    _priv, pub = _kp()
    peer_fp = "a" * 32
    mirror_admission(peer_fp, "some-operator", pub)

    devices = _devices(kernel_home, peer_fp)
    assert devices
    assert getattr(devices[0].mode, "value", devices[0].mode) == "tofu"
