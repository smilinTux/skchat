"""Tests for skchat.skreach.protocol — §1.3 verification pipeline.

Covers acceptance criteria:
  SIG-1: invalid signature → dropped, sig_invalid
  SIG-2: expired envelope → dropped, expired_cmd
  SIG-3: replay attack → dropped, replay_cmd (same id, second delivery)
  SIG-4: misdirected envelope (sub != self) → dropped, misdirected_cmd
  + unauthorized issuer (guest role) → dropped, unauthorized_iss
  + fresh valid envelope → VerifyResult.valid == True
"""

from __future__ import annotations

import secrets
import time

from skchat.skreach.protocol import (
    DropReason,
    _ReplayCache,
    verify_envelope,
)

from .conftest import (
    _make_envelope,
    _role_guest,
    _role_operator,
    _role_owner,
    _sig_always_invalid,
    _sig_always_valid,
)

SELF_FQID = "noroc2027@chef.skworld.io"


# ---------------------------------------------------------------------------
# SIG-1: invalid signature is dropped immediately
# ---------------------------------------------------------------------------


def test_sig_invalid_dropped(fresh_replay_cache: _ReplayCache) -> None:
    """SIG-1: envelope with bad signature → dropped, no processing."""
    env = _make_envelope()
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_invalid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
    )
    assert not result.valid
    assert result.reason == DropReason.SIG_INVALID
    # Must never appear in the replay cache (dropped before insertion)
    assert not fresh_replay_cache.check_and_insert(env.id, env.exp)


# ---------------------------------------------------------------------------
# SIG-2: expired envelope is dropped
# ---------------------------------------------------------------------------


def test_expired_envelope_dropped(fresh_replay_cache: _ReplayCache) -> None:
    """SIG-2: exp < now → expired_cmd (with zero tolerance + clock skew)."""
    now = time.time()
    env = _make_envelope(iat=now - 400, exp=now - 50)  # expired 50s ago
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
        now=now,
    )
    assert not result.valid
    assert result.reason == DropReason.EXPIRED


def test_barely_expired_within_skew_still_valid(fresh_replay_cache: _ReplayCache) -> None:
    """An envelope expired by less than MAX_CLOCK_SKEW_S (5s) is still accepted."""
    now = time.time()
    # Expired by 3 seconds but within the 5s clock skew window
    env = _make_envelope(iat=now - 303, exp=now - 3)
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
        now=now,
    )
    assert result.valid


def test_expired_by_one_second_past_skew_dropped(fresh_replay_cache: _ReplayCache) -> None:
    """Expired by 6s (1s past the 5s clock skew) → rejected."""
    now = time.time()
    env = _make_envelope(iat=now - 306, exp=now - 6)
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
        now=now,
    )
    assert not result.valid
    assert result.reason == DropReason.EXPIRED


# ---------------------------------------------------------------------------
# SIG-3: replay attack is blocked
# ---------------------------------------------------------------------------


def test_replay_same_id_blocked(fresh_replay_cache: _ReplayCache) -> None:
    """SIG-3: second delivery of the same cmd id within cache window → replay_cmd."""
    env = _make_envelope()
    now = time.time()
    # First delivery — should succeed
    r1 = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
        now=now,
    )
    assert r1.valid

    # Second delivery — same id, same envelope → replay_cmd
    r2 = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
        now=now,
    )
    assert not r2.valid
    assert r2.reason == DropReason.REPLAY


def test_different_id_not_replay(fresh_replay_cache: _ReplayCache) -> None:
    """Two envelopes with different ids are both accepted (no false positive)."""
    now = time.time()
    env1 = _make_envelope(cmd_id=secrets.token_hex(16))
    env2 = _make_envelope(cmd_id=secrets.token_hex(16))

    r1 = verify_envelope(
        env1, SELF_FQID, _sig_always_valid, _role_owner, replay_cache=fresh_replay_cache, now=now
    )
    r2 = verify_envelope(
        env2, SELF_FQID, _sig_always_valid, _role_owner, replay_cache=fresh_replay_cache, now=now
    )
    assert r1.valid
    assert r2.valid


# ---------------------------------------------------------------------------
# SIG-4: misdirected envelope (sub != self_fqid)
# ---------------------------------------------------------------------------


def test_misdirected_envelope_dropped(fresh_replay_cache: _ReplayCache) -> None:
    """SIG-4: envelope addressed to a different node → misdirected_cmd."""
    env = _make_envelope(sub="chiap04@chef.skworld.io")  # NOT our FQID
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,  # we are noroc2027
        sig_verifier=_sig_always_valid,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
    )
    assert not result.valid
    assert result.reason == DropReason.MISDIRECTED


# ---------------------------------------------------------------------------
# Unauthorised issuer (guest role)
# ---------------------------------------------------------------------------


def test_guest_issuer_dropped(fresh_replay_cache: _ReplayCache) -> None:
    """Guest-role issuer → unauthorized_iss (dropped after sig/freshness/replay pass)."""
    env = _make_envelope(iss="stranger@nowhere.io")
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_guest,  # always resolves to guest
        replay_cache=fresh_replay_cache,
    )
    assert not result.valid
    assert result.reason == DropReason.UNAUTHORIZED_ISS


# ---------------------------------------------------------------------------
# Happy path: valid envelope passes all checks
# ---------------------------------------------------------------------------


def test_valid_envelope_passes_all_checks(fresh_replay_cache: _ReplayCache) -> None:
    """A fully valid envelope resolves with valid=True and the correct role."""
    env = _make_envelope()
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=_role_operator,
        replay_cache=fresh_replay_cache,
    )
    assert result.valid
    assert result.reason == DropReason.OK
    assert result.role == "operator"


def test_verify_result_carries_role(fresh_replay_cache: _ReplayCache) -> None:
    """verify_envelope() carries the server-resolved role in the result."""
    env = _make_envelope(iss="lumina@chef.skworld.io")
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_sig_always_valid,
        role_resolver=lambda _iss: "agent",
        replay_cache=fresh_replay_cache,
    )
    assert result.valid
    assert result.role == "agent"


# ---------------------------------------------------------------------------
# Verifier exception safety
# ---------------------------------------------------------------------------


def test_verifier_exception_treated_as_invalid(fresh_replay_cache: _ReplayCache) -> None:
    """If sig_verifier raises, the envelope is treated as SIG_INVALID."""

    def _crashy(_env: CommandEnvelope) -> bool:
        raise RuntimeError("PGP backend unavailable")

    env = _make_envelope()
    result = verify_envelope(
        envelope=env,
        self_fqid=SELF_FQID,
        sig_verifier=_crashy,
        role_resolver=_role_owner,
        replay_cache=fresh_replay_cache,
    )
    assert not result.valid
    assert result.reason == DropReason.SIG_INVALID
