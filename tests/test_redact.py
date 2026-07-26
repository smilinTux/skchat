"""Unit tests for skchat.redact -- mask_fqid(), mask_fingerprint(), mask_ip(), and mask_email()."""

from __future__ import annotations

from skchat.redact import REDACTED_PLACEHOLDER, mask_email, mask_fingerprint, mask_fqid, mask_ip

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


# ---------------------------------------------------------------------------
# mask_ip
# ---------------------------------------------------------------------------


class TestMaskIp:
    def test_typical_ipv4(self):
        assert mask_ip("192.168.0.41") == "***.***.***.41"

    def test_ipv4_with_port(self):
        assert mask_ip("192.168.0.41:8080") == "***.***.***.41:8080"

    def test_loopback(self):
        assert mask_ip("127.0.0.1") == "***.***.***.1"

    def test_none_input(self):
        assert mask_ip(None) == REDACTED_PLACEHOLDER

    def test_empty_string(self):
        assert mask_ip("") == REDACTED_PLACEHOLDER

    def test_whitespace_only(self):
        assert mask_ip("   ") == REDACTED_PLACEHOLDER

    def test_non_string_input(self):
        assert mask_ip(12345) == REDACTED_PLACEHOLDER

    def test_malformed_too_few_octets(self):
        assert mask_ip("192.168.0") == REDACTED_PLACEHOLDER

    def test_malformed_too_many_octets(self):
        assert mask_ip("192.168.0.41.5") == REDACTED_PLACEHOLDER

    def test_malformed_octet_out_of_range(self):
        assert mask_ip("192.168.0.999") == REDACTED_PLACEHOLDER

    def test_malformed_non_numeric_octet(self):
        assert mask_ip("192.168.0.abc") == REDACTED_PLACEHOLDER

    def test_hostname_not_ipv4(self):
        assert mask_ip("example.com") == REDACTED_PLACEHOLDER

    def test_hostname_with_port_not_ipv4(self):
        assert mask_ip("example.com:8080") == REDACTED_PLACEHOLDER

    def test_malformed_port(self):
        assert mask_ip("192.168.0.41:notaport") == REDACTED_PLACEHOLDER


# ---------------------------------------------------------------------------
# mask_email
# ---------------------------------------------------------------------------


class TestMaskEmail:
    def test_typical_email(self):
        assert mask_email("alice@x.com") == "a***e@x.com"

    def test_longer_local_part(self):
        assert mask_email("chef@skworld.io") == "c**f@skworld.io"

    def test_short_local_part_two_chars(self):
        # Can't show first+last distinctly without revealing everything -> fully mask.
        assert mask_email("ab@x.com") == "**@x.com"

    def test_single_char_local_part(self):
        assert mask_email("a@x.com") == "*@x.com"

    def test_none_input(self):
        assert mask_email(None) == REDACTED_PLACEHOLDER

    def test_empty_string(self):
        assert mask_email("") == REDACTED_PLACEHOLDER

    def test_whitespace_only(self):
        assert mask_email("   ") == REDACTED_PLACEHOLDER

    def test_malformed_no_at_sign(self):
        assert mask_email("not-an-email") == REDACTED_PLACEHOLDER

    def test_malformed_multiple_at_signs(self):
        assert mask_email("a@b@c") == REDACTED_PLACEHOLDER

    def test_malformed_no_domain(self):
        assert mask_email("alice@") == REDACTED_PLACEHOLDER

    def test_malformed_no_local_part(self):
        assert mask_email("@x.com") == REDACTED_PLACEHOLDER

    def test_non_string_input(self):
        assert mask_email(12345) == REDACTED_PLACEHOLDER
