"""Unit tests for skchat.guest — invite create/verify + LiveKit grant builder.

All tests inject a deterministic clock and a fixed secret so they never touch
real env vars, never make network calls, and never require livekit-api to be
installed (the build_livekit_token tests are skipped gracefully if absent).

Coverage targets
----------------
- Invite token creation: shape, TTL cap, URL construction
- Verify: happy path, expiry, tamper, wrong room, wrong tier, revocation
- GuestToken: identity assignment (server-assigned; guest cannot choose)
- build_livekit_token: not called when livekit-api absent (skip guard)
- guest_join_page_html: XSS-escape of room name + token
- revoke_invite: revoked JTI rejected on next verify
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

# ── Fixtures / helpers ────────────────────────────────────────────────────────

_SECRET = "test-secret-do-not-use-in-production"
_ROOM = "lumina-and-chef"
_DISPLAY = "Alice"


class _FixedClock:
    """Injected clock that returns a controllable timestamp.

    Defaults to the real current time so that minted JWTs are not
    immediately expired when PyJWT verifies them against wall-clock time.
    Tests that need to test expiry advance the clock explicitly.
    """

    def __init__(self, t: float | None = None) -> None:
        self.t = t if t is not None else time.time()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_issuer(clock: _FixedClock):
    from skchat.guest import InviteIssuer

    return InviteIssuer(secret=_SECRET, now_fn=clock)


def _make_verifier(clock: _FixedClock):
    from skchat.guest import InviteVerifier

    return InviteVerifier(secret=_SECRET, now_fn=clock)


# ── InviteIssuer ──────────────────────────────────────────────────────────────

class TestInviteIssuer:
    def test_create_returns_expected_keys(self):
        clk = _FixedClock()
        result = _make_issuer(clk).create_invite(_ROOM)
        assert "invite_token" in result
        assert "invite_url" in result
        assert "jti" in result
        assert result["room"] == _ROOM

    def test_invite_token_is_decodable_jwt(self):
        import jwt as _jwt

        clk = _FixedClock()
        result = _make_issuer(clk).create_invite(_ROOM)
        payload = _jwt.decode(
            result["invite_token"], _SECRET, algorithms=["HS256"]
        )
        assert payload["room"] == _ROOM
        assert payload["tier"] == "invite"
        assert "jti" in payload

    def test_ttl_default_applied(self):
        import jwt as _jwt

        clk = _FixedClock()
        result = _make_issuer(clk).create_invite(_ROOM)
        # Decode without expiry verification so the test is clock-independent.
        payload = _jwt.decode(
            result["invite_token"], _SECRET, algorithms=["HS256"],
            options={"verify_exp": False},
        )
        default_ttl = 14400
        assert payload["exp"] == pytest.approx(clk.t + default_ttl, abs=5)

    def test_ttl_capped_at_max(self):
        import jwt as _jwt

        from skchat.guest import _MAX_INVITE_TTL

        clk = _FixedClock()
        result = _make_issuer(clk).create_invite(_ROOM, ttl=999_999)
        payload = _jwt.decode(
            result["invite_token"], _SECRET, algorithms=["HS256"],
            options={"verify_exp": False},
        )
        assert payload["exp"] <= clk.t + _MAX_INVITE_TTL + 1

    def test_different_invites_have_unique_jti(self):
        clk = _FixedClock()
        issuer = _make_issuer(clk)
        jtis = {issuer.create_invite(_ROOM)["jti"] for _ in range(10)}
        assert len(jtis) == 10

    def test_invite_url_contains_room_and_token(self, monkeypatch):
        monkeypatch.setenv("SKCHAT_FUNNEL_PUBLIC_URL", "https://example.ts.net:10000")
        clk = _FixedClock()
        result = _make_issuer(clk).create_invite("my-room")
        assert "my-room" in result["invite_url"]
        assert result["invite_token"] in result["invite_url"]

    def test_missing_room_raises(self):
        clk = _FixedClock()
        with pytest.raises(ValueError, match="room"):
            _make_issuer(clk).create_invite("")

    def test_missing_secret_raises(self, monkeypatch):
        monkeypatch.delenv("SKCHAT_GUEST_TOKEN_SECRET", raising=False)
        from skchat.guest import InviteIssuer

        issuer = InviteIssuer()  # no injected secret → reads env
        with pytest.raises(RuntimeError, match="SKCHAT_GUEST_TOKEN_SECRET"):
            issuer.create_invite(_ROOM)


# ── InviteVerifier ────────────────────────────────────────────────────────────

class TestInviteVerifier:
    def _create_token(self, clock, room=_ROOM, ttl=3600, display=_DISPLAY):
        return _make_issuer(clock).create_invite(room, display=display, ttl=ttl)

    # ── happy path ──────────────────────────────────────────────────────────

    def test_verify_returns_guest_token(self):
        from skchat.guest import GuestToken

        clk = _FixedClock()
        info = self._create_token(clk)
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert isinstance(gt, GuestToken)
        assert gt.room == _ROOM

    def test_identity_is_server_assigned(self):
        """Guest cannot choose their LiveKit identity — always guest:<jti[:8]>."""
        clk = _FixedClock()
        info = self._create_token(clk)
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert gt.identity.startswith("guest:")
        assert gt.identity == f"guest:{info['jti'][:8]}"

    def test_display_name_from_body(self):
        clk = _FixedClock()
        info = self._create_token(clk, display="tokenHint")
        gt = _make_verifier(clk).verify(
            info["invite_token"], expected_room=_ROOM, display_name="BodyName"
        )
        assert gt.display == "BodyName"

    def test_display_name_falls_back_to_token_hint(self):
        clk = _FixedClock()
        info = self._create_token(clk, display="TokenHint")
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert gt.display == "TokenHint"

    def test_display_name_defaults_to_guest(self):
        clk = _FixedClock()
        info = self._create_token(clk, display="")
        gt = _make_verifier(clk).verify(
            info["invite_token"], expected_room=_ROOM, display_name=""
        )
        assert gt.display == "Guest"

    def test_display_name_truncated_to_40(self):
        clk = _FixedClock()
        info = self._create_token(clk)
        long_name = "A" * 100
        gt = _make_verifier(clk).verify(
            info["invite_token"], expected_room=_ROOM, display_name=long_name
        )
        assert len(gt.display) == 40

    def test_exp_matches_invite_token(self):
        clk = _FixedClock()
        info = self._create_token(clk, ttl=7200)
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert gt.exp == pytest.approx(clk.t + 7200, abs=2)

    def test_perms_include_publish_audio_and_camera(self):
        clk = _FixedClock()
        info = self._create_token(clk)
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert "publish_audio" in gt.perms
        assert "publish_camera" in gt.perms

    # ── expiry ──────────────────────────────────────────────────────────────

    def test_expired_token_rejected(self):
        """A token with exp in the past is rejected by PyJWT's expiry check."""
        import jwt as _jwt

        from skchat.guest import GuestJoinError

        # Mint a token with exp already in the past (iat and exp both historical).
        past = 1_000_000.0  # 1970 + ~11 days — safely in the past
        payload = {
            "jti": "expired001",
            "iss": "operator",
            "room": _ROOM,
            "display": "",
            "iat": int(past),
            "exp": int(past + 300),  # still in the past from wall-clock perspective
            "tier": "invite",
        }
        token = _jwt.encode(payload, _SECRET, algorithm="HS256")
        clk = _FixedClock()
        with pytest.raises(GuestJoinError):
            _make_verifier(clk).verify(token, expected_room=_ROOM)

    def test_not_yet_expired_accepted(self):
        clk = _FixedClock()
        info = self._create_token(clk, ttl=3600)
        # Should not raise — token was just minted with a 1h TTL.
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        assert gt.room == _ROOM

    # ── tamper ──────────────────────────────────────────────────────────────

    def test_wrong_secret_rejected(self):
        from skchat.guest import GuestJoinError, InviteVerifier

        clk = _FixedClock()
        info = _make_issuer(clk).create_invite(_ROOM)
        verifier_bad = InviteVerifier(secret="wrong-secret", now_fn=clk)
        with pytest.raises(GuestJoinError):
            verifier_bad.verify(info["invite_token"], expected_room=_ROOM)

    def test_truncated_token_rejected(self):
        from skchat.guest import GuestJoinError

        clk = _FixedClock()
        info = self._create_token(clk)
        bad_token = info["invite_token"][:-10]  # chop signature bytes
        with pytest.raises(GuestJoinError):
            _make_verifier(clk).verify(bad_token, expected_room=_ROOM)

    def test_garbage_token_rejected(self):
        from skchat.guest import GuestJoinError

        clk = _FixedClock()
        with pytest.raises(GuestJoinError):
            _make_verifier(clk).verify("not.a.jwt", expected_room=_ROOM)

    # ── room scope ──────────────────────────────────────────────────────────

    def test_wrong_room_rejected(self):
        """A token minted for room A cannot be used to join room B."""
        from skchat.guest import GuestJoinError

        clk = _FixedClock()
        info = self._create_token(clk, room="room-a")
        with pytest.raises(GuestJoinError, match="room mismatch"):
            _make_verifier(clk).verify(info["invite_token"], expected_room="room-b")

    def test_correct_room_accepted(self):
        clk = _FixedClock()
        info = self._create_token(clk, room="special-room")
        gt = _make_verifier(clk).verify(info["invite_token"], expected_room="special-room")
        assert gt.room == "special-room"

    # ── wrong tier ──────────────────────────────────────────────────────────

    def test_non_invite_tier_rejected(self):
        """A token with tier != 'invite' (e.g. a different JWT from this system) is rejected."""
        import jwt as _jwt

        from skchat.guest import GuestJoinError, InviteVerifier

        clk = _FixedClock()
        future_exp = int(clk.t) + 3600
        payload = {
            "jti": "aabbccdd",
            "iss": "operator",
            "room": _ROOM,
            "iat": int(clk.t),
            "exp": future_exp,
            "tier": "session",  # wrong tier
        }
        token = _jwt.encode(payload, _SECRET, algorithm="HS256")
        with pytest.raises(GuestJoinError, match="not an invite token"):
            InviteVerifier(secret=_SECRET, now_fn=clk).verify(token, expected_room=_ROOM)

    # ── revocation ──────────────────────────────────────────────────────────

    def test_revoked_token_rejected(self):
        from skchat.guest import GuestJoinError, revoke_invite

        clk = _FixedClock()
        info = self._create_token(clk)
        jti = info["jti"]
        revoke_invite(jti)
        try:
            with pytest.raises(GuestJoinError, match="revoked"):
                _make_verifier(clk).verify(info["invite_token"], expected_room=_ROOM)
        finally:
            # Clean up module-level state so other tests are unaffected.
            from skchat.guest import _revoked_jtis
            _revoked_jtis.discard(jti)

    def test_different_jti_not_affected_by_revocation(self):
        from skchat.guest import revoke_invite

        clk = _FixedClock()
        info_a = self._create_token(clk)
        info_b = self._create_token(clk)
        revoke_invite(info_a["jti"])
        try:
            # Token B should still be valid.
            gt = _make_verifier(clk).verify(info_b["invite_token"], expected_room=_ROOM)
            assert gt.jti == info_b["jti"]
        finally:
            from skchat.guest import _revoked_jtis
            _revoked_jtis.discard(info_a["jti"])


# ── guest_join_page_html ──────────────────────────────────────────────────────

class TestGuestJoinPageHtml:
    def test_contains_room_name(self):
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html("my-room", "tok123")
        assert "my-room" in page

    def test_contains_invite_token(self):
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html("room", "mytoken")
        assert "mytoken" in page

    def test_xss_room_name_escaped(self):
        """A room name with HTML chars must be escaped wherever it appears in the page."""
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html('<img src=x onerror=alert(1)>', "tok")
        # The literal tag must not appear unescaped in the output.
        assert "<img src=x" not in page
        # The escaped form must be present (appears in <title> and <h1> and value=).
        assert "&lt;img" in page

    def test_xss_invite_token_escaped(self):
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html("room", '"><img src=x onerror=alert(1)>')
        assert "<img" not in page
        assert "&gt;" in page

    def test_error_block_shown_when_provided(self):
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html("room", "tok", error="Something went wrong")
        assert "Something went wrong" in page

    def test_error_block_absent_when_empty(self):
        """The server-rendered error block must be absent when no error is passed.

        The page's inline JS contains the string literal '<p class="err">' for
        dynamic error insertion — that's fine. We check that the Python-rendered
        error_block variable (which sits between the .agents paragraph and the
        <form>) is empty, i.e. no extra <p> tag appears in the static HTML between
        those two landmarks.
        """
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html("room", "tok")
        # The error_block variable renders as {error_block} between the agents
        # paragraph and the form. When empty it's just whitespace.
        # Check: no <p> element appears right before the <form>.
        # The static section between agents and form must be whitespace-only.
        import re
        between = re.search(
            r'<p class="agents">.*?</p>(.*?)<form', page, re.DOTALL
        )
        assert between is not None, "Expected agents paragraph and form in page"
        between_text = between.group(1).strip()
        assert between_text == "", (
            f"Expected no server-rendered content between agents p and form, got: {between_text!r}"
        )

    def test_error_xss_escaped(self):
        from skchat.guest import guest_join_page_html

        page = guest_join_page_html("room", "tok", error="<b>bad</b>")
        assert "<b>" not in page
        assert "&lt;b&gt;" in page


# ── build_livekit_token ───────────────────────────────────────────────────────

class TestBuildLivekitToken:
    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("livekit"),
        reason="livekit-api not installed",
    )
    def test_minted_token_is_string(self):
        from skchat.guest import GuestToken, build_livekit_token

        clk = _FixedClock()  # defaults to wall-clock now
        gt = GuestToken(
            jti="aabb1122",
            room="test-room",
            identity="guest:aabb1122",
            display="Tester",
            exp=clk.t + 3600,
        )
        # Requires real livekit API creds; use dummy values — token mint
        # itself does not validate the key format, just HMAC-signs the JWT.
        token = build_livekit_token(
            gt,
            livekit_api_key="test-key",
            livekit_api_secret="test-secret-long-enough-for-livekit",
            now_fn=clk,
        )
        assert isinstance(token, str)
        assert len(token) > 20

    def test_import_error_raised_when_livekit_absent(self):
        """When livekit-api is not installed, ImportError propagates."""
        import sys

        from skchat.guest import GuestToken, build_livekit_token

        clk = _FixedClock()
        gt = GuestToken(
            jti="aabb1122",
            room="test-room",
            identity="guest:aabb1122",
            display="Tester",
            exp=clk.t + 3600,
        )
        # Temporarily make livekit unimportable.
        with patch.dict(sys.modules, {"livekit": None, "livekit.api": None}):
            with pytest.raises((ImportError, Exception)):
                build_livekit_token(
                    gt,
                    livekit_api_key="key",
                    livekit_api_secret="secret",
                    now_fn=clk,
                )


# ── GuestToken identity invariant (identity spoofing guard) ──────────────────

class TestIdentitySpoofingGuard:
    def test_guest_cannot_claim_operator_identity(self):
        """A guest who POSTs display_name='chef@skworld.io' still gets guest:<jti>."""
        clk = _FixedClock()
        info = _make_issuer(clk).create_invite(_ROOM, display="")
        gt = _make_verifier(clk).verify(
            info["invite_token"],
            expected_room=_ROOM,
            display_name="chef@skworld.io",  # attacker tries to claim operator name
        )
        # Identity is always server-assigned; display name is separate.
        assert gt.identity.startswith("guest:")
        assert "chef" not in gt.identity
        # Display name is stored separately (and is just cosmetic).
        assert gt.display == "chef@skworld.io"

    def test_guest_identity_prefix_is_always_guest(self):
        """Identity string never starts with a capauth FQID or operator handle."""
        clk = _FixedClock()
        for display in [
            "lumina@chef.skworld.io",
            "operator",
            "admin",
            "capauth:opus@skworld.io",
        ]:
            info = _make_issuer(clk).create_invite(_ROOM)
            gt = _make_verifier(clk).verify(
                info["invite_token"], expected_room=_ROOM, display_name=display
            )
            assert gt.identity.startswith("guest:"), f"identity={gt.identity!r}"
