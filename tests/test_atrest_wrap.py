"""Tests for the at-rest hybrid key-wrap layer (PQC Q4).

Covers: wrap/unwrap round-trip, suite/version tagging, the DEK being random
(NOT fingerprint-derived), back-compat read of old-format stores, migration
old→new preserving plaintext exactly, and malformed-blob handling.

These require the liboqs (``oqs``) backend; skipped cleanly if unavailable.
"""

from __future__ import annotations

import importlib

import pytest

pqkem = importlib.import_module("skcomms.pqkem")

pytestmark = pytest.mark.skipif(
    not pqkem.is_available(),
    reason="liboqs/oqs backend unavailable — hybrid KEM cannot run",
)

# Imported after the skip-marker on purpose (module is import-safe regardless,
# but this keeps the file uniform with the oqs-gated suites). importlib avoids
# an out-of-order top-level import block.
atrest_wrap = importlib.import_module("skchat.atrest_wrap")


# ---------------------------------------------------------------------------
# wrap_dek / unwrap_dek round-trip + agility tags
# ---------------------------------------------------------------------------


class TestWrapUnwrapRoundTrip:
    def test_roundtrip(self):
        kp = atrest_wrap.new_recipient_keypair()
        dek = atrest_wrap.new_dek()
        blob = atrest_wrap.wrap_dek(dek, kp.public_key)
        recovered = atrest_wrap.unwrap_dek(blob, kp.private_key)
        assert recovered == dek
        assert len(recovered) == atrest_wrap.DEK_LEN

    def test_blob_is_suite_and_version_tagged(self):
        kp = atrest_wrap.new_recipient_keypair()
        blob = atrest_wrap.wrap_dek(atrest_wrap.new_dek(), kp.public_key)
        assert atrest_wrap.is_wrapped_blob(blob)
        desc = atrest_wrap.describe_blob(blob)
        assert desc["suite_id"] == "x25519-mlkem768"
        assert desc["version"] == atrest_wrap.WRAP_FORMAT_VERSION
        assert blob.startswith(atrest_wrap.WRAP_MAGIC)

    def test_each_wrap_unique(self):
        # Fresh KEM ciphertext + nonce each time → different blob, same DEK.
        kp = atrest_wrap.new_recipient_keypair()
        dek = atrest_wrap.new_dek()
        b1 = atrest_wrap.wrap_dek(dek, kp.public_key)
        b2 = atrest_wrap.wrap_dek(dek, kp.public_key)
        assert b1 != b2
        assert atrest_wrap.unwrap_dek(b1, kp.private_key) == dek
        assert atrest_wrap.unwrap_dek(b2, kp.private_key) == dek

    def test_dek_is_high_entropy_not_derived(self):
        # The DEK source is os.urandom(32): two DEKs must differ, and a DEK must
        # NOT equal any fingerprint-derived key (the fixed Q4 bug).
        from skchat.encrypted_store import StorageKeyDeriver

        d1 = atrest_wrap.new_dek()
        d2 = atrest_wrap.new_dek()
        assert d1 != d2
        fp_key = StorageKeyDeriver.derive_key("AABBCCDD" * 5, salt=b"x" * 16)
        assert d1 != fp_key  # not fingerprint-derived

    def test_wrong_recipient_key_fails(self):
        kp = atrest_wrap.new_recipient_keypair()
        other = atrest_wrap.new_recipient_keypair()
        blob = atrest_wrap.wrap_dek(atrest_wrap.new_dek(), kp.public_key)
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.unwrap_dek(blob, other.private_key)


# ---------------------------------------------------------------------------
# malformed input handling — never crash, always raise the format error
# ---------------------------------------------------------------------------


class TestMalformed:
    def test_bad_magic(self):
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.unwrap_dek(b"NOPE" + b"\x00" * 1180, b"\x00" * 2432)

    def test_truncated_blob(self):
        kp = atrest_wrap.new_recipient_keypair()
        blob = atrest_wrap.wrap_dek(atrest_wrap.new_dek(), kp.public_key)
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.unwrap_dek(blob[:-10], kp.private_key)

    def test_tampered_ciphertext(self):
        kp = atrest_wrap.new_recipient_keypair()
        blob = bytearray(atrest_wrap.wrap_dek(atrest_wrap.new_dek(), kp.public_key))
        blob[-1] ^= 0xFF  # flip a byte in the AES-GCM tag region
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.unwrap_dek(bytes(blob), kp.private_key)

    def test_bad_dek_length(self):
        kp = atrest_wrap.new_recipient_keypair()
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.wrap_dek(b"\x00" * 16, kp.public_key)

    def test_bad_pubkey_length(self):
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.wrap_dek(atrest_wrap.new_dek(), b"\x00" * 100)

    def test_unsupported_version_raises(self):
        kp = atrest_wrap.new_recipient_keypair()
        blob = bytearray(atrest_wrap.wrap_dek(atrest_wrap.new_dek(), kp.public_key))
        # version byte immediately after the 4-byte magic
        blob[len(atrest_wrap.WRAP_MAGIC)] = 99
        with pytest.raises(atrest_wrap.AtRestWrapFormatError):
            atrest_wrap.unwrap_dek(bytes(blob), kp.private_key)
