"""Reuse cache for the operator-audience issue path (runaway-mint fix).

``issue_operator_audience`` ran on EVERY session handshake and minted a fresh
capauth audience token each time, with no reuse. Each mint stores a file, so the
store flooded (38k files / 153MB of expired 12h-TTL tokens). A per-fingerprint
cache reuses a still-valid token until shortly before expiry, mirroring the shadow
twin's cache, and GC is nudged after a real mint. The token itself is unchanged.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from skchat import operator_audience as oa


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv(oa.OPERATOR_AUDIENCE_ISSUE_FLAG, "1")
    oa._operator_audience_cache.clear()
    monkeypatch.setattr(oa, "_gc_token_store", lambda: None)  # isolate GC by default
    yield
    oa._operator_audience_cache.clear()


def _fake_token(fp: str, ttl_hours: int = 12):
    from capauth.tokens import SignedToken, TokenPayload, TokenType

    now = datetime.now(timezone.utc)
    payload = TokenPayload(
        token_id="t" + fp,
        token_type=TokenType.CAPABILITY,
        issuer="AABB",
        subject=oa.operator_subject(fp),
        capabilities=["chat.read"],
        expires_at=now + timedelta(hours=ttl_hours),
        audience="skchat",
    )
    return SignedToken(payload=payload)


def _counting_mint(counter: dict):
    def _mint(fp: str):
        counter["n"] = counter.get("n", 0) + 1
        return _fake_token(fp)

    return _mint


def test_second_issue_within_ttl_reuses_cache_no_second_mint(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(oa, "mint_operator_audience_token", _counting_mint(calls))
    r1 = oa.issue_operator_audience("abc123")
    r2 = oa.issue_operator_audience("abc123")
    assert calls["n"] == 1  # minted once, second call served from cache
    assert r1 == r2
    assert r1 and r1["audience_token"]


def test_cache_within_refresh_skew_of_expiry_remints(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(oa, "mint_operator_audience_token", _counting_mint(calls))
    oa.issue_operator_audience("abc123")
    wire, exp_iso, _ = oa._operator_audience_cache["abc123"]
    # push the cached entry to just inside the refresh skew so it must re-mint
    oa._operator_audience_cache["abc123"] = (
        wire,
        exp_iso,
        time.time() + oa._AUDIENCE_REFRESH_SKEW - 1,
    )
    oa.issue_operator_audience("abc123")
    assert calls["n"] == 2


def test_different_fingerprints_do_not_share_cache(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(oa, "mint_operator_audience_token", _counting_mint(calls))
    oa.issue_operator_audience("fp-A")
    oa.issue_operator_audience("fp-B")
    assert calls["n"] == 2


def test_flag_off_returns_none_and_never_mints(monkeypatch):
    monkeypatch.delenv(oa.OPERATOR_AUDIENCE_ISSUE_FLAG, raising=False)
    calls: dict = {}
    monkeypatch.setattr(oa, "mint_operator_audience_token", _counting_mint(calls))
    assert oa.issue_operator_audience("abc") is None
    assert calls.get("n", 0) == 0


def test_mint_failure_is_non_fatal_and_not_cached(monkeypatch):
    def boom(fp: str):
        raise RuntimeError("keyring locked")

    monkeypatch.setattr(oa, "mint_operator_audience_token", boom)
    assert oa.issue_operator_audience("abc") is None
    assert "abc" not in oa._operator_audience_cache


def test_gc_is_nudged_after_a_real_mint(monkeypatch):
    monkeypatch.setattr(oa, "mint_operator_audience_token", lambda fp: _fake_token(fp))
    hit: dict = {}
    monkeypatch.setattr(oa, "_gc_token_store", lambda: hit.__setitem__("n", hit.get("n", 0) + 1))
    oa.issue_operator_audience("abc")
    assert hit.get("n") == 1
