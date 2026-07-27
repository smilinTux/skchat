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
