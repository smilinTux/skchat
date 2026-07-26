"""Unit tests for skchat.redact -- mask_fqid() and mask_fingerprint()."""

from __future__ import annotations

from skchat.redact import REDACTED_PLACEHOLDER, mask_fingerprint, mask_fqid

# ---------------------------------------------------------------------------
# mask_fqid
# ---------------------------------------------------------------------------


class TestMaskFqid:
    def test_typical_fqid(self):
        assert mask_fqid("lumina@skworld.io") == "l****a@skworld.io"

    def test_capauth_uri(self):
        assert mask_fqid("capauth:lumina@skworld.io") == "capauth:l****a@skworld.io"

    def test_short_local_part_two_chars(self):
        # Can't show first+last distinctly without revealing everything -> fully mask.
        assert mask_fqid("ab@skworld.io") == "**@skworld.io"

    def test_single_char_local_part(self):
        assert mask_fqid("a@skworld.io") == "*@skworld.io"

    def test_none_input(self):
        assert mask_fqid(None) == REDACTED_PLACEHOLDER

    def test_empty_string(self):
        assert mask_fqid("") == REDACTED_PLACEHOLDER

    def test_whitespace_only(self):
        assert mask_fqid("   ") == REDACTED_PLACEHOLDER

    def test_malformed_no_at_sign(self):
        assert mask_fqid("not-an-fqid") == REDACTED_PLACEHOLDER

    def test_malformed_multiple_at_signs(self):
        assert mask_fqid("a@b@c") == REDACTED_PLACEHOLDER

    def test_non_string_input(self):
        assert mask_fqid(12345) == REDACTED_PLACEHOLDER


# ---------------------------------------------------------------------------
# mask_fingerprint
# ---------------------------------------------------------------------------


class TestMaskFingerprint:
    def test_typical_fingerprint(self):
        fp = "ABCD1234EFGH5678IJKL9012MNOP3456QRST7890"
        masked = mask_fingerprint(fp)
        assert masked.endswith("QRST7890")
        assert masked == "*" * (len(fp) - 8) + "QRST7890"

    def test_exactly_eight_chars(self):
        # Nothing to mask without revealing the whole thing -> fully mask.
        assert mask_fingerprint("ABCD1234") == "*" * 8

    def test_shorter_than_eight_chars(self):
        assert mask_fingerprint("ABCD") == "****"

    def test_none_input(self):
        assert mask_fingerprint(None) == REDACTED_PLACEHOLDER

    def test_empty_string(self):
        assert mask_fingerprint("") == REDACTED_PLACEHOLDER

    def test_whitespace_only(self):
        assert mask_fingerprint("   ") == REDACTED_PLACEHOLDER

    def test_non_string_input(self):
        assert mask_fingerprint(12345) == REDACTED_PLACEHOLDER
