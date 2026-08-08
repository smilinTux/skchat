"""Shadow mode: verify and report, but store anyway.

The point of shadow is to answer "who breaks if I flip this" from the log before
anything is rejected. Two properties matter and both are tested here:

  1. shadow NEVER rejects (a bad bundle is still stored), and
  2. shadow actually VERIFIES (a good bundle logs ACCEPT, a bad one logs REJECT
     with a reason). A shadow mode that logged REJECT for everything would make
     the soak worthless, which is the specific failure this guards.

PGP only, no liboqs needed.
"""

from __future__ import annotations

import importlib
import logging

import pytest

from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"
ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    """pq_prekeys bound to an isolated SKCHAT_HOME (fresh peer store)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def alice_crypto(alice_keys: tuple[str, str]) -> ChatCrypto:
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


@pytest.fixture()
def unsigned_bundle() -> dict:
    pub_hex = "ab" * 32
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": "chef-web",
        "ratchet": "pqdr1",
    }


# --------------------------------------------------------------------------- #
# reason codes
# --------------------------------------------------------------------------- #


def test_reason_unsigned(PQ, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    assert PQ._prekey_verify_reason(unsigned_bundle, alice_pub) == "unsigned"


def test_reason_no_signer_key(PQ, alice_crypto, unsigned_bundle):
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    assert PQ._prekey_verify_reason(signed, None) == "no-signer-key"


def test_reason_bad_signature(PQ, alice_crypto, bob_keys, unsigned_bundle):
    """Signed by alice, verified against bob's key."""
    _, bob_pub = bob_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    assert PQ._prekey_verify_reason(signed, bob_pub) == "bad-signature"


def test_reason_none_when_valid(PQ, alice_crypto, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    assert PQ._prekey_verify_reason(signed, alice_pub) is None


# --------------------------------------------------------------------------- #
# shadow stores anyway
# --------------------------------------------------------------------------- #


def test_shadow_stores_an_unsigned_bundle(PQ, monkeypatch, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    monkeypatch.setenv(ENV, "shadow")

    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle, signer_public_armor=alice_pub)

    assert stored is True, "shadow must never reject"
    assert PQ.load_peer_bundle("chef") is not None


def test_enforce_rejects_the_same_bundle(PQ, monkeypatch, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    monkeypatch.setenv(ENV, "1")

    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle, signer_public_armor=alice_pub)

    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_off_stores_without_verifying(PQ, monkeypatch, unsigned_bundle):
    monkeypatch.delenv(ENV, raising=False)

    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle, signer_public_armor=None)

    assert stored is True
    assert PQ.load_peer_bundle("chef") is not None


# --------------------------------------------------------------------------- #
# shadow actually verifies (the soak-is-meaningful property)
# --------------------------------------------------------------------------- #


def test_shadow_logs_reject_with_reason(PQ, monkeypatch, caplog, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle(
            "chef", unsigned_bundle, signer_public_armor=alice_pub, signer_source="daemon-attest"
        )

    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "mode=shadow" in line
    assert "owner=chef" in line
    assert "result=REJECT" in line
    assert "reason=unsigned" in line
    assert "signer=daemon-attest" in line


def test_shadow_logs_accept_for_a_valid_bundle(
    PQ, monkeypatch, caplog, alice_crypto, alice_keys, unsigned_bundle
):
    """The property that makes a soak meaningful: a GOOD bundle logs ACCEPT.

    If the signer were never resolved in shadow, everything would log REJECT and
    the soak would tell us nothing.
    """
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle(
            "chef", signed, signer_public_armor=alice_pub, signer_source="daemon-attest"
        )

    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=ACCEPT" in line
    assert "reason=" not in line, "an accept carries no reason code"


def test_log_never_contains_key_material(
    PQ, monkeypatch, caplog, alice_crypto, alice_keys, unsigned_bundle
):
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle("chef", signed, signer_public_armor=alice_pub)

    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert signed["hybrid_public_hex"] not in line
    assert str(signed["signature"]) not in line
    assert "BEGIN PGP" not in line
    assert signed["key_id"][:8] in line, "the truncated key_id IS logged"


def test_off_mode_logs_nothing(PQ, monkeypatch, caplog, unsigned_bundle):
    monkeypatch.delenv(ENV, raising=False)

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle("chef", unsigned_bundle)

    assert not [r for r in caplog.records if "prekey-verify" in r.getMessage()]


# --------------------------------------------------------------------------- #
# ACCEPT log level: shadow escalates to WARNING, enforce stays at INFO
#
# uvicorn.run(log_level="warning") in the webui process only configures the
# uvicorn* loggers; the root logger is left at WARNING with no handlers, so an
# INFO record from this module is silently dropped in production. Shadow mode
# escalates ACCEPT to WARNING so the rollout soak (step 4: "every distinct
# publishing device should appear with result=ACCEPT") is actually visible.
# --------------------------------------------------------------------------- #


def test_shadow_accept_is_logged_at_warning(
    PQ, monkeypatch, caplog, alice_crypto, alice_keys, unsigned_bundle
):
    """A valid bundle's ACCEPT record must be WARNING in shadow, or it is invisible

    to a production webui process (uvicorn configures only uvicorn* loggers, so
    root stays at WARNING with no handlers). Pinning the level, not just the
    message text, is the point: a future "tidy this back to info" edit must fail
    this test.
    """
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle(
            "chef", signed, signer_public_armor=alice_pub, signer_source="daemon-attest"
        )

    record = next(r for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=ACCEPT" in record.getMessage()
    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"


def test_enforce_accept_is_logged_at_info(
    PQ, monkeypatch, caplog, alice_crypto, alice_keys, unsigned_bundle
):
    """Same valid bundle, but in enforce: ACCEPT stays at INFO (steady-state).

    Pinned alongside the shadow test above so the shadow/enforce distinction
    itself, not just each mode in isolation, is guarded against drifting back
    to a single uniform level.
    """
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    monkeypatch.setenv(ENV, "1")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle(
            "chef", signed, signer_public_armor=alice_pub, signer_source="daemon-attest"
        )

    record = next(r for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=ACCEPT" in record.getMessage()
    assert record.levelno == logging.INFO
    assert record.levelname == "INFO"
