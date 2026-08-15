"""Pure poll/answer core of the 1:1 auto-answer service.

The network + LiveKit layers sit behind the `AnswererApi` seam, so these tests
inject a fake and never touch live HTTP or LiveKit.
"""

from skchat.call_answerer import _should_leave, poll_and_answer


def test_should_leave_stays_while_peer_present():
    assert _should_leave(1, True, 0.0, 10.0) is False


def test_should_leave_when_peer_hangs_up():
    # A peer was seen, now the room is empty: caller hung up.
    assert _should_leave(0, True, 1.0, 30.0) is True


def test_should_leave_after_alone_timeout_when_nobody_joins():
    assert _should_leave(0, False, 5.0, 5.0, alone_timeout_s=45) is False
    assert _should_leave(0, False, 45.0, 45.0, alone_timeout_s=45) is True


def test_should_leave_at_hard_call_cap_even_with_peer():
    assert _should_leave(1, True, 0.0, 3600.0, max_call_s=3600) is True


class _FakeApi:
    def __init__(self, invites):
        self._invites = invites
        self.answered = []

    def poll_incoming(self):
        return self._invites

    def answer(self, peer):
        self.answered.append(peer)
        return {"room": "call-abc", "token": "tok", "livekit_url": "wss://sfu"}


def test_answers_newest_unseen_invite():
    api = _FakeApi(
        [
            {
                "from_fqid": "lumina@chef.skworld.io",
                "room": "call-abc",
                "livekit_url": "wss://sfu",
                "nonce": "n1",
                "ts": 5,
            },
        ]
    )
    seen = set()
    res = poll_and_answer(api, seen)
    assert api.answered == ["lumina@chef.skworld.io"]
    assert res["room"] == "call-abc" and res["token"] == "tok"
    assert "n1" in seen


def test_answers_only_the_newest_when_several_pending():
    api = _FakeApi(
        [
            {
                "from_fqid": "a@chef.skworld.io",
                "room": "r1",
                "livekit_url": "w",
                "nonce": "old",
                "ts": 1,
            },
            {
                "from_fqid": "b@chef.skworld.io",
                "room": "r2",
                "livekit_url": "w",
                "nonce": "new",
                "ts": 9,
            },
        ]
    )
    seen = set()
    res = poll_and_answer(api, seen)
    # Newest (ts=9) is answered; both are marked seen so the older, now-stale
    # invite is not picked up on a later cycle.
    assert api.answered == ["b@chef.skworld.io"]
    assert res["room"] == "call-abc"
    assert "new" in seen and "old" in seen


def test_dedupes_by_nonce():
    api = _FakeApi(
        [
            {
                "from_fqid": "lumina@chef.skworld.io",
                "room": "r",
                "livekit_url": "w",
                "nonce": "n1",
                "ts": 5,
            },
        ]
    )
    seen = {"n1"}
    res = poll_and_answer(api, seen)
    assert res is None and api.answered == []


def test_no_invites_is_noop():
    api = _FakeApi([])
    assert poll_and_answer(api, set()) is None


def test_invite_without_nonce_is_ignored():
    # Unsigned/anti-spoof-stripped bodies never carry a nonce; skip them.
    api = _FakeApi(
        [
            {"from_fqid": "x@chef.skworld.io", "room": "r", "livekit_url": "w", "ts": 5},
        ]
    )
    assert poll_and_answer(api, set()) is None


def test_conversational_peer_hands_the_verified_fqid_through_untouched():
    """The invite FQID is the only trustworthy fact about who is calling.

    This used to rewrite a self-addressed call (from == to) to the literal
    string "chef", because the mode ceiling was keyed on a name prefix. A bare
    name is not an identity anything can verify, and substituting one threw
    away the FQID the signature actually covers. Resolution now happens in
    caller_profile, which knows that an invite from the agent to itself came
    from the operator: the /call routes are operator-gated.
    """
    from skchat.call_answerer import _conversational_peer

    self_addressed = {"peer_fqid": "lumina@chef.skworld.io", "room": "call-abc"}
    assert _conversational_peer(self_addressed) == "lumina@chef.skworld.io"

    from_only = {"from_fqid": "nan@chef.skworld.io"}
    assert _conversational_peer(from_only) == "nan@chef.skworld.io"

    assert _conversational_peer({"room": "call-abc"}) is None


def test_answered_invite_carries_the_verified_caller_into_the_session(monkeypatch):
    """End of the trust chain: signed invite -> joinable -> caller profile.

    /call/incoming has already cross-checked the invite against the signed
    envelope sender, so from_fqid is the input the session privilege decision
    is allowed to use, and it must survive the answer step intact.
    """
    from skchat.call_answerer import _conversational_peer
    from skchat.voice_engine.caller_profile import (
        CallerProfile,
        reset_default_directory,
        resolve_caller_profile,
    )

    monkeypatch.setenv("SKCHAT_AGENT_FQID", "lumina@chef.skworld.io")
    monkeypatch.setenv("SKCHAT_COMPANION_FQIDS", "nan@chef.skworld.io")
    reset_default_directory()
    try:
        api = _FakeApi(
            [
                {
                    "from_fqid": "lumina@chef.skworld.io",
                    "room": "call-abc",
                    "livekit_url": "wss://sfu",
                    "nonce": "n1",
                    "ts": 10,
                }
            ]
        )
        joinable = poll_and_answer(api, set())
        assert joinable["peer_fqid"] == "lumina@chef.skworld.io"
        peer = _conversational_peer(joinable)
        assert resolve_caller_profile(peer) is CallerProfile.OPERATOR
    finally:
        reset_default_directory()
