"""Pure poll/answer core of the 1:1 auto-answer service.

The network + LiveKit layers sit behind the `AnswererApi` seam, so these tests
inject a fake and never touch live HTTP or LiveKit.
"""
from skchat.call_answerer import poll_and_answer


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
    api = _FakeApi([
        {"from_fqid": "lumina@chef.skworld.io", "room": "call-abc",
         "livekit_url": "wss://sfu", "nonce": "n1", "ts": 5},
    ])
    seen = set()
    res = poll_and_answer(api, seen)
    assert api.answered == ["lumina@chef.skworld.io"]
    assert res["room"] == "call-abc" and res["token"] == "tok"
    assert "n1" in seen


def test_answers_only_the_newest_when_several_pending():
    api = _FakeApi([
        {"from_fqid": "a@chef.skworld.io", "room": "r1",
         "livekit_url": "w", "nonce": "old", "ts": 1},
        {"from_fqid": "b@chef.skworld.io", "room": "r2",
         "livekit_url": "w", "nonce": "new", "ts": 9},
    ])
    seen = set()
    res = poll_and_answer(api, seen)
    # Newest (ts=9) is answered; both are marked seen so the older, now-stale
    # invite is not picked up on a later cycle.
    assert api.answered == ["b@chef.skworld.io"]
    assert res["room"] == "call-abc"
    assert "new" in seen and "old" in seen


def test_dedupes_by_nonce():
    api = _FakeApi([
        {"from_fqid": "lumina@chef.skworld.io", "room": "r",
         "livekit_url": "w", "nonce": "n1", "ts": 5},
    ])
    seen = {"n1"}
    res = poll_and_answer(api, seen)
    assert res is None and api.answered == []


def test_no_invites_is_noop():
    api = _FakeApi([])
    assert poll_and_answer(api, set()) is None


def test_invite_without_nonce_is_ignored():
    # Unsigned/anti-spoof-stripped bodies never carry a nonce; skip them.
    api = _FakeApi([
        {"from_fqid": "x@chef.skworld.io", "room": "r", "livekit_url": "w", "ts": 5},
    ])
    assert poll_and_answer(api, set()) is None
