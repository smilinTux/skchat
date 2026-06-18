"""Guest security hardening tests — persistent revocation, single-use invites,
anti-spoof verified attribute + reserved display-name guard.

Covers coord task ff744786:
  1. Persistent revocation — a revoked JTI survives a simulated process restart
     (fresh issuer/verifier + cache reset, same SQLite DB).
  2. Anti-spoof — guest LiveKit tokens carry ``verified=false``; reserved display
     names ("Chef", etc.) are suffixed so a guest cannot impersonate an operator.
  3. Opt-in single-use — a ``single_use`` invite works exactly once, then 401s;
     default (multi-use) invites stay reusable until expiry.

All tests use an isolated tmp SQLite DB (via the autouse conftest fixture that
sets SKCHAT_GUEST_REVOCATION_DB), inject a deterministic clock + fixed secret,
and never touch the real ~/.skchat or any network.
"""

from __future__ import annotations

import importlib.util
import time

import pytest

_SECRET = "test-secret-do-not-use-in-production"
_ROOM = "lumina-and-chef"
_HAVE_LIVEKIT = importlib.util.find_spec("livekit") is not None


class _FixedClock:
    """Controllable clock; defaults to wall-clock now so JWTs aren't pre-expired."""

    def __init__(self, t: float | None = None) -> None:
        self.t = t if t is not None else time.time()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _issuer(clock):
    from skchat.guest import InviteIssuer

    return InviteIssuer(secret=_SECRET, now_fn=clock)


def _verifier(clock):
    from skchat.guest import InviteVerifier

    return InviteVerifier(secret=_SECRET, now_fn=clock)


# ── 1. Persistent revocation ──────────────────────────────────────────────────


class TestPersistentRevocation:
    def test_revocation_written_to_sqlite(self, monkeypatch, tmp_path):
        """revoke_invite() lands a row in the SQLite store (source of truth)."""
        import sqlite3

        import skchat.guest as guest

        db = tmp_path / "rev.db"
        monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(db))
        guest._reset_revocation_cache()

        guest.revoke_invite("deadbeef")

        assert db.exists()
        rows = sqlite3.connect(str(db)).execute("SELECT jti FROM revoked_jtis").fetchall()
        assert ("deadbeef",) in rows

    def test_revocation_survives_restart(self, monkeypatch, tmp_path):
        """A revoked JTI is still rejected after a simulated process restart.

        "Restart" = drop the in-memory cache and build a fresh verifier; only the
        on-disk SQLite row can carry the revocation across that boundary.
        """
        import skchat.guest as guest
        from skchat.guest import GuestJoinError

        db = tmp_path / "rev.db"
        monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(db))
        guest._reset_revocation_cache()

        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM, ttl=3600)
        guest.revoke_invite(info["jti"])

        # --- simulate restart: wipe the in-memory cache entirely ---
        guest._reset_revocation_cache()
        assert info["jti"] not in guest._revoked_cache  # cache really is empty

        # Fresh verifier instance re-reads the DB → still revoked.
        with pytest.raises(GuestJoinError, match="revoked"):
            _verifier(_FixedClock(clk.t)).verify(info["invite_token"], expected_room=_ROOM)

    def test_unrevoked_token_unaffected_after_restart(self, monkeypatch, tmp_path):
        import skchat.guest as guest
        from skchat.guest import GuestToken

        db = tmp_path / "rev.db"
        monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(db))
        guest._reset_revocation_cache()

        clk = _FixedClock()
        good = _issuer(clk).create_invite(_ROOM, ttl=3600)
        bad = _issuer(clk).create_invite(_ROOM, ttl=3600)
        guest.revoke_invite(bad["jti"])

        guest._reset_revocation_cache()  # restart
        gt = _verifier(_FixedClock(clk.t)).verify(good["invite_token"], expected_room=_ROOM)
        assert isinstance(gt, GuestToken)
        assert gt.jti == good["jti"]


# ── 2. Anti-spoof: reserved display names + verified attribute ─────────────────


class TestReservedDisplayNames:
    @pytest.mark.parametrize(
        "name", ["Chef", "chef", "CHEF", "Lumina", "admin", "host", "sovereign"]
    )
    def test_reserved_name_suffixed(self, name):
        """A guest who picks a reserved operator/agent name is suffixed '(guest)'."""
        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM)
        gt = _verifier(clk).verify(info["invite_token"], expected_room=_ROOM, display_name=name)
        assert gt.display.endswith("(guest)")
        # The bare reserved handle must NOT be the rendered display name.
        assert gt.display.strip().lower() != name.strip().lower()

    def test_ordinary_name_unchanged(self):
        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM)
        gt = _verifier(clk).verify(info["invite_token"], expected_room=_ROOM, display_name="Alice")
        assert gt.display == "Alice"

    def test_operator_email_not_reserved(self):
        """'chef@skworld.io' is not the bare reserved token 'chef' — left intact.

        (Identity is server-assigned regardless; this only governs the cosmetic
        display name, and matches the legacy test_guest.py expectation.)
        """
        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM)
        gt = _verifier(clk).verify(
            info["invite_token"], expected_room=_ROOM, display_name="chef@skworld.io"
        )
        assert gt.display == "chef@skworld.io"
        assert gt.identity.startswith("guest:")


@pytest.mark.skipif(not _HAVE_LIVEKIT, reason="livekit-api not installed")
class TestVerifiedAttribute:
    def test_guest_token_carries_verified_false(self):
        """The minted guest LiveKit JWT stamps attributes.verified == 'false'."""
        import jwt as _jwt

        from skchat.guest import GuestToken, build_livekit_token

        clk = _FixedClock()
        gt = GuestToken(
            jti="aabb1122",
            room="conf-room",
            identity="guest:aabb1122",
            display="Tester",
            exp=clk.t + 3600,
        )
        token = build_livekit_token(
            gt,
            livekit_api_key="test-key",
            livekit_api_secret="test-secret-long-enough-for-livekit",
            now_fn=clk,
        )
        claims = _jwt.decode(token, "test-secret-long-enough-for-livekit", algorithms=["HS256"])
        assert claims.get("attributes", {}).get("verified") == "false"


# ── 3. Opt-in single-use invites ──────────────────────────────────────────────


class TestSingleUseInvites:
    def test_single_use_flag_in_result_and_claim(self):
        import jwt as _jwt

        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM, ttl=3600, single_use=True)
        assert info["single_use"] is True
        payload = _jwt.decode(
            info["invite_token"], _SECRET, algorithms=["HS256"], options={"verify_exp": False}
        )
        assert payload.get("once") is True

    def test_single_use_works_once_then_rejected(self):
        from skchat.guest import GuestJoinError

        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM, ttl=3600, single_use=True)

        # First use succeeds.
        gt = _verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert gt.single_use is True
        assert gt.room == _ROOM

        # Second use of the SAME invite is rejected.
        with pytest.raises(GuestJoinError, match="already used"):
            _verifier(clk).verify(info["invite_token"], expected_room=_ROOM)

    def test_single_use_burn_survives_restart(self, monkeypatch, tmp_path):
        """A burned single-use invite stays burned across a simulated restart."""
        import skchat.guest as guest
        from skchat.guest import GuestJoinError

        db = tmp_path / "rev.db"
        monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(db))
        guest._reset_revocation_cache()

        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM, ttl=3600, single_use=True)
        _verifier(clk).verify(info["invite_token"], expected_room=_ROOM)  # burn

        guest._reset_revocation_cache()  # restart (used-table is DB-backed)
        with pytest.raises(GuestJoinError, match="already used"):
            _verifier(_FixedClock(clk.t)).verify(info["invite_token"], expected_room=_ROOM)

    def test_default_invite_is_multi_use(self):
        """Default (no single_use) invites stay reusable until expiry — no regression."""
        clk = _FixedClock()
        info = _issuer(clk).create_invite(_ROOM, ttl=3600)
        assert info["single_use"] is False

        v = _verifier(clk)
        gt1 = v.verify(info["invite_token"], expected_room=_ROOM)
        gt2 = v.verify(info["invite_token"], expected_room=_ROOM)
        gt3 = v.verify(info["invite_token"], expected_room=_ROOM)
        assert gt1.jti == gt2.jti == gt3.jti
        assert gt1.single_use is False

    def test_single_use_expired_still_rejected_by_expiry(self):
        """An expired single-use token is rejected by JWT expiry (not the used-table)."""
        import jwt as _jwt

        from skchat.guest import GuestJoinError

        past = 1_000_000.0
        payload = {
            "jti": "onceexpired",
            "iss": "operator",
            "room": _ROOM,
            "display": "",
            "iat": int(past),
            "exp": int(past + 300),
            "tier": "invite",
            "once": True,
        }
        token = _jwt.encode(payload, _SECRET, algorithm="HS256")
        with pytest.raises(GuestJoinError):
            _verifier(_FixedClock()).verify(token, expected_room=_ROOM)
