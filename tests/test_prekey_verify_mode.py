"""Tri-state parse for SKCHAT_REQUIRE_SIGNED_PREKEYS (off / shadow / enforce).

Mirrors the repo's existing rollout idiom, dataplane_auth.authz_pdp_mode(): read
at call time so an operator can stage a rollout without a reimport, and anything
unrecognized reads as the safe default. The historical truthy values keep meaning
enforce so no existing reader changes behaviour.
"""

from __future__ import annotations

import pytest

from skchat import pq_prekeys as PQ

ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"


def test_unset_is_off(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert PQ.prekey_verify_mode() == "off"
    assert PQ.require_signed_prekeys() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_historical_truthy_values_mean_enforce(monkeypatch, value):
    """Back-compat: every value that used to enable the gate still enforces."""
    monkeypatch.setenv(ENV, value)
    assert PQ.prekey_verify_mode() == "enforce"
    assert PQ.require_signed_prekeys() is True


@pytest.mark.parametrize("value", ["shadow", "SHADOW", " shadow "])
def test_shadow_is_its_own_mode(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    assert PQ.prekey_verify_mode() == "shadow"
    assert PQ.require_signed_prekeys() is False, "shadow must never reject"


@pytest.mark.parametrize("value", ["", "0", "off", "no", "banana", "enforce-ish"])
def test_unrecognized_reads_as_off(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    assert PQ.prekey_verify_mode() == "off"
    assert PQ.require_signed_prekeys() is False


def test_mode_is_read_at_call_time(monkeypatch):
    """No reimport needed to stage a rollout."""
    monkeypatch.delenv(ENV, raising=False)
    assert PQ.prekey_verify_mode() == "off"
    monkeypatch.setenv(ENV, "shadow")
    assert PQ.prekey_verify_mode() == "shadow"
    monkeypatch.setenv(ENV, "1")
    assert PQ.prekey_verify_mode() == "enforce"
