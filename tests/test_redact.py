"""Unit tests for skchat.redact -- mask_fqid(), mask_fingerprint(), mask_ip(), mask_email(),
mask_token(), mask_query_params(), scrub(), and redact_dict()."""

from __future__ import annotations

from skchat.redact import (
    REDACTED_PLACEHOLDER,
    mask_email,
    mask_fingerprint,
    mask_fqid,
    mask_ip,
    mask_query_params,
    mask_token,
    redact_dict,
    scrub,
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


# ---------------------------------------------------------------------------
# scrub
# ---------------------------------------------------------------------------


class TestScrub:
    def test_line_with_multiple_mixed_secrets(self):
        line = (
            "peer 192.168.0.41:8080 (capauth:lumina@skworld.io, alice@x.com) "
            "key AABB1122CCDD3344EEFF5566AABB1122CCDD3344 connected"
        )
        scrubbed = scrub(line)
        assert scrubbed == (
            "peer ***.***.***.41:8080 (capauth:l****a@skworld.io, a***e@x.com) "
            "key ********************************CCDD3344 connected"
        )
        assert "192.168.0.41" not in scrubbed
        assert "lumina@skworld.io" not in scrubbed
        assert "alice@x.com" not in scrubbed
        assert "AABB1122CCDD3344EEFF5566AABB1122CCDD3344" not in scrubbed

    def test_clean_line_unchanged(self):
        line = "daemon started, polling inbox every 5 seconds"
        assert scrub(line) == line

    def test_empty_string(self):
        assert scrub("") == ""

    def test_none_input(self):
        assert scrub(None) == ""

    def test_non_string_input(self):
        assert scrub(12345) == ""


# ---------------------------------------------------------------------------
# redact_dict
# ---------------------------------------------------------------------------


class TestRedactDict:
    def test_mixed_value_dict(self):
        mapping = {
            "peer": "lumina@skworld.io",
            "ip": "192.168.0.41",
            "count": 3,
            "ok": True,
            "note": None,
        }
        assert redact_dict(mapping) == {
            "peer": "l****a@skworld.io",
            "ip": "***.***.***.41",
            "count": 3,
            "ok": True,
            "note": None,
        }

    def test_nested_dict(self):
        mapping = {
            "sender": "chef@skworld.io",
            "meta": {
                "remote": "192.168.0.41:8080",
                "inner": {"fqid": "capauth:lumina@skworld.io"},
            },
        }
        assert redact_dict(mapping) == {
            "sender": "c**f@skworld.io",
            "meta": {
                "remote": "***.***.***.41:8080",
                "inner": {"fqid": "capauth:l****a@skworld.io"},
            },
        }

    def test_non_string_values_copied_unchanged(self):
        mapping = {"n": 42, "f": 1.5, "lst": ["chef@skworld.io"], "none": None, "flag": False}
        result = redact_dict(mapping)
        assert result == mapping
        assert result["lst"] is mapping["lst"]

    def test_clean_string_value_unchanged(self):
        assert redact_dict({"msg": "daemon started"}) == {"msg": "daemon started"}

    def test_empty_dict(self):
        assert redact_dict({}) == {}

    def test_none_input(self):
        assert redact_dict(None) == {}

    def test_non_mapping_input(self):
        assert redact_dict("not-a-dict") == {}
        assert redact_dict(12345) == {}
        assert redact_dict(["a", "b"]) == {}

    def test_does_not_mutate_input(self):
        mapping = {"peer": "lumina@skworld.io", "nested": {"ip": "192.168.0.41"}}
        original = {"peer": "lumina@skworld.io", "nested": {"ip": "192.168.0.41"}}
        redact_dict(mapping)
        assert mapping == original

    def test_returns_new_dict_not_same_object(self):
        mapping = {"a": "clean text"}
        result = redact_dict(mapping)
        assert result is not mapping
        assert result["a"] == "clean text"


# ---------------------------------------------------------------------------
# mask_query_params
# ---------------------------------------------------------------------------


class TestMaskQueryParams:
    def test_single_sensitive_param(self):
        url = "https://api.example.com/v1/status?token=abc123"
        assert mask_query_params(url) == "https://api.example.com/v1/status?token=<redacted>"

    def test_each_recognized_param_name(self):
        for name in (
            "token",
            "key",
            "apikey",
            "api_key",
            "password",
            "passwd",
            "pwd",
            "secret",
            "sig",
            "signature",
            "access_token",
            "auth",
        ):
            url = f"https://x.example.com/path?{name}=verysecretvalue"
            assert mask_query_params(url) == f"https://x.example.com/path?{name}=<redacted>", name

    def test_case_insensitive_param_name_preserves_original_case(self):
        url = "https://x.example.com/path?Token=abc123&API_KEY=xyz"
        expected = "https://x.example.com/path?Token=<redacted>&API_KEY=<redacted>"
        assert mask_query_params(url) == expected

    def test_mixed_sensitive_and_non_sensitive_params(self):
        url = "https://x.example.com/search?q=hello&token=abc123&limit=10&sig=deadbeef"
        assert mask_query_params(url) == (
            "https://x.example.com/search?q=hello&token=<redacted>&limit=10&sig=<redacted>"
        )

    def test_preserves_param_order(self):
        url = "https://x.example.com/p?b=2&password=secret&a=1"
        assert mask_query_params(url) == "https://x.example.com/p?b=2&password=<redacted>&a=1"

    def test_preserves_fragment(self):
        url = "https://x.example.com/p?token=abc123#section"
        assert mask_query_params(url) == "https://x.example.com/p?token=<redacted>#section"

    def test_no_query_string_unchanged(self):
        url = "https://x.example.com/path"
        assert mask_query_params(url) == url

    def test_no_sensitive_params_unchanged(self):
        url = "https://x.example.com/search?q=hello&limit=10"
        assert mask_query_params(url) == url

    def test_empty_string(self):
        assert mask_query_params("") == REDACTED_PLACEHOLDER

    def test_none_input(self):
        assert mask_query_params(None) == REDACTED_PLACEHOLDER

    def test_non_string_input(self):
        assert mask_query_params(12345) == REDACTED_PLACEHOLDER
