"""Unit tests for skchat.redact -- mask_fqid(), mask_fingerprint(), mask_ip(), mask_email(),
and mask_token()."""

from __future__ import annotations

from skchat.redact import (
    REDACTED_PLACEHOLDER,
    mask_email,
    mask_fingerprint,
    mask_fqid,
    mask_ip,
    mask_token,
)

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


# ---------------------------------------------------------------------------
# mask_token
# ---------------------------------------------------------------------------


class TestMaskToken:
    def test_anthropic_key(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        assert mask_token(key) == "sk-ant-<redacted>"

    def test_npm_token(self):
        token = "npm_1234567890abcdefghijklmnopqrstuvwxyz"
        assert mask_token(token) == "npm_<redacted>"

    def test_github_personal_access_token(self):
        token = "ghp_1234567890abcdefghijklmnopqrstuvwx"
        assert mask_token(token) == "ghp_<redacted>"

    def test_github_oauth_token(self):
        token = "gho_1234567890abcdefghijklmnopqrstuvwx"
        assert mask_token(token) == "gho_<redacted>"

    def test_github_server_token(self):
        token = "ghs_1234567890abcdefghijklmnopqrstuvwx"
        assert mask_token(token) == "ghs_<redacted>"

    def test_aws_access_key_id(self):
        assert mask_token("AKIAIOSFODNN7EXAMPLE") == "AKIA<redacted>"

    def test_standalone_hex_run(self):
        run = "deadbeefcafebabe0123456789abcdef"
        assert mask_token(run) == "deadbe<redacted>"

    def test_standalone_hex_run_below_min_length_unchanged(self):
        # Below the 24-char floor, don't guess -- too likely to be a plain id.
        run = "deadbeefca"
        assert mask_token(run) == run

    def test_standalone_base64_like_run(self):
        run = "QwErTy1234ZxCvBnM7890LkJh"
        assert mask_token(run) == "QwErTy<redacted>"

    def test_plain_lowercase_word_unchanged(self):
        # Same length class as a token but no digit/case mix -> not flagged.
        word = "abcdefghijklmnopqrstuvwxyz"
        assert mask_token(word) == word

    def test_pure_digit_run_is_valid_hex(self):
        # Decimal digits are a subset of hex chars, so a long digit run
        # (e.g. a numeric secret) is still caught by the hex-run check.
        run = "123456789012345678901234"
        assert mask_token(run) == "123456<redacted>"

    def test_plain_sentence_unchanged(self):
        text = "the quick brown fox jumps over the lazy dog and eats"
        assert mask_token(text) == text

    def test_none_input(self):
        assert mask_token(None) == REDACTED_PLACEHOLDER

    def test_empty_string(self):
        assert mask_token("") == REDACTED_PLACEHOLDER

    def test_whitespace_only(self):
        assert mask_token("   ") == REDACTED_PLACEHOLDER

    def test_non_string_input(self):
        assert mask_token(12345) == REDACTED_PLACEHOLDER
