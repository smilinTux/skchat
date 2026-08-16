"""M2 guest-store fold: durable admissions mirror into capauth.pairing, fail-safe.

A durable ConsumedNonces store (real db path) dual-writes every admission, trust,
and revocation into capauth.pairing. In-memory stores never mirror. The mirror is
best-effort: a capauth error can never break the SQLite admission.

Subjects here are CANONICAL fqids (``sk-standards`` IDENTITY_NAMING_STANDARD,
ratified 2026-08-14): a device seat is ``device:<hex fingerprint>`` and an
operator is ``<name>@<operator>.<org-domain>``. ``capauth.pairing.enroll_device``
refuses anything it cannot translate, and the old placeholders here (``peerfp1``,
``op@host``) were neither hex nor fqid-shaped, so nothing could translate them.
Because the mirror swallows every capauth error by design, that refusal showed
up as an EMPTY store rather than as a raised error.

That is also why the negative tests below (in-memory, kernel-off) now use
canonical subjects: asserting an empty store under a subject capauth would
refuse anyway makes them pass for the wrong reason, certifying nothing. See
``tests/test_canonical_fqid_regression.py``.
"""

from __future__ import annotations

import pytest

from skchat.guest_accept import ConsumedNonces

#: Device seats, canonical form: ``device:<16-64 lowercase hex>``. Shaped like
#: the real thing, a 40-char PGP fingerprint (``guest_accept.pubkey_fingerprint``).
PEER_1 = "device:" + "a1" * 20
PEER_3 = "device:" + "a3" * 20
PEER_4 = "device:" + "a4" * 20
PEER_5 = "device:" + "a5" * 20
PEER_X = "device:" + "af" * 20

#: Operator ids. ``record_admission``'s ``operator_id`` is documented as "the
#: peer's operator FQID", so it takes the agent/human fqid grammar.
OPERATOR = "op@chef.skworld.io"
OPERATOR_2 = "op2@chef.skworld.io"
OPERATOR_X = "opx@chef.skworld.io"


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
    store.record_admission(PEER_1, OPERATOR, "{}", "sigA", "sigB")
    devs = _devices(PEER_1, base)
    assert len(devs) == 1
    assert devs[0].mode.value == "tofu"
    assert devs[0].subject == PEER_1
    assert not devs[0].revoked


def test_trust_operator_mirrors_attested_device(durable_store):
    # KNOWN RED, and NOT a naming defect: with the subject now canonical this
    # test still fails, and the swallowed capauth error is
    #   "attested enrollment requires 'operator_pubkey' + 'attestation': a
    #    signature by the vouching operator's key over the fingerprint+subject
    #    challenge (card N10)"
    # skchat.pairing_mirror.mirror_trusted_operator never builds that
    # attestation, so no trusted operator has been mirrored since capauth
    # started requiring it. Fixing that is a separate change (the mirror has no
    # vouching key to sign with); do NOT "fix" it by relaxing the subject.
    store, base = durable_store
    store.trust_operator(OPERATOR_2, "PUBKEY-ARMOR")
    devs = _devices(OPERATOR_2, base)
    assert len(devs) == 1
    assert devs[0].mode.value == "attested"


def test_revocation_mirrors_revoke(durable_store):
    store, base = durable_store
    store.record_admission(PEER_3, OPERATOR, "{}", "s1", "s2")
    assert not _devices(PEER_3, base)[0].revoked
    store.revoke_pin(PEER_3)
    assert all(d.revoked for d in _devices(PEER_3, base))


def test_in_memory_store_never_mirrors(tmp_path, monkeypatch):
    # The existing guest tests use :memory: and must produce zero capauth writes.
    # The subjects are canonical on purpose: an empty store here has to mean
    # "the mirror was skipped", not "capauth refused the subject".
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    store = ConsumedNonces(db_path=":memory:")
    store.record_admission(PEER_X, OPERATOR, "{}", "s1", "s2")
    store.trust_operator(OPERATOR_X, "PUB")
    assert _devices(PEER_X, base) == []
    assert _devices(OPERATOR_X, base) == []


def test_mirror_failure_never_breaks_admission(tmp_path, monkeypatch):
    monkeypatch.delenv("SKCHAT_PAIRING_KERNEL", raising=False)
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", str(tmp_path / "capauth"))

    def _boom(*a, **k):
        raise RuntimeError("capauth down")

    monkeypatch.setattr("capauth.pairing.enroll_device", _boom)
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    # The admission must still succeed in SQLite despite the mirror blowing up.
    store.record_admission(PEER_4, OPERATOR, "{}", "s1", "s2")
    assert store.is_admitted(PEER_4) is True
    store.close()


def test_kernel_disabled_skips_mirror(tmp_path, monkeypatch):
    # Canonical subject again: the kernel flag must be what stops the write.
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL", "0")
    base = str(tmp_path / "capauth")
    monkeypatch.setenv("SKCHAT_PAIRING_KERNEL_BASE", base)
    store = ConsumedNonces(db_path=str(tmp_path / "nonces.db"))
    store.record_admission(PEER_5, OPERATOR, "{}", "s1", "s2")
    assert _devices(PEER_5, base) == []  # kernel off -> no mirror
    store.close()
