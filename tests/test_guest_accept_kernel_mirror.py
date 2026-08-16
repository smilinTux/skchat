"""M2 guest-store fold: durable admissions mirror into capauth.pairing, fail-safe.

A durable ConsumedNonces store (real db path) dual-writes every admission, trust,
and revocation into capauth.pairing. In-memory stores never mirror. The mirror is
best-effort: a capauth error can never break the SQLite admission.
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
    store.record_admission("peerfp1", "op@host", "{}", "sigA", "sigB")
    devs = _devices("peerfp1", base)
    assert len(devs) == 1
    assert devs[0].mode.value == "tofu"
    assert devs[0].subject == "peerfp1"
    assert not devs[0].revoked


def test_trust_operator_mirrors_attested_device(durable_store):
    store, base = durable_store
    store.trust_operator("op2@host", "PUBKEY-ARMOR")
    devs = _devices("op2@host", base)
    assert len(devs) == 1
    assert devs[0].mode.value == "attested"


def test_revocation_mirrors_revoke(durable_store):
    store, base = durable_store
    store.record_admission("peerfp3", "op@host", "{}", "s1", "s2")
    assert not _devices("peerfp3", base)[0].revoked
    store.revoke_pin("peerfp3")
    assert all(d.revoked for d in _devices("peerfp3", base))


def test_in_memory_store_never_mirrors(tmp_path, monkeypatch):
    # The existing guest tests use :memory: and must produce zero capauth writes.
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    store = ConsumedNonces(db_path=":memory:")
    store.record_admission("peerfpX", "op@host", "{}", "s1", "s2")
    store.trust_operator("opX@host", "PUB")
    assert _devices("peerfpX", base) == []
    assert _devices("opX@host", base) == []


def test_mirror_failure_never_breaks_admission(tmp_path, monkeypatch):
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", str(tmp_path / "capauth"))

    def _boom(*a, **k):
        raise RuntimeError("capauth down")

    monkeypatch.setattr("capauth.pairing.enroll_device", _boom)
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    # The admission must still succeed in SQLite despite the mirror blowing up.
    store.record_admission("peerfp4", "op@host", "{}", "s1", "s2")
    assert store.is_admitted("peerfp4") is True
    store.close()


def test_kernel_disabled_skips_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL", "0")
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    store.record_admission("peerfp5", "op@host", "{}", "s1", "s2")
    assert _devices("peerfp5", base) == []  # kernel off -> no mirror
    store.close()
