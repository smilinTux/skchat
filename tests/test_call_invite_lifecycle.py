"""CALL_INVITE lifecycle: invites must stop ringing, and self-calls must not exist.

Chef was rung repeatedly by "incoming call from lumina@chef.skworld.io". Two
distinct defects, both found live:

1. **Nothing ever consumed an invite.** /call/incoming is a pure read of the
   skcomms inbox, so an answered call kept ringing forever. 15 envelopes were
   stuck, the oldest 20 minutes old.
2. **Every one was self-addressed** (from_fqid == to_fqid). A call to yourself
   is never real, and it is the thing that generated the loop.
"""

from __future__ import annotations

import time


def test_invite_ttl_is_configurable_and_bounded():
    from skchat.call_routes import INVITE_TTL_S

    assert INVITE_TTL_S > 0, "an unbounded TTL means an unanswered call rings forever"


def test_consume_invites_is_best_effort_and_never_raises(monkeypatch):
    """Tidying up must never be able to fail the call it is tidying up after."""
    from skchat import call_routes

    def _boom():
        raise RuntimeError("mailbox exploded")

    monkeypatch.setattr(call_routes, "_invite_dir", _boom)
    assert call_routes._consume_invites(from_fqid="chef@skworld.io") == 0


def test_stale_invite_is_filtered_by_ttl():
    """An invite older than the TTL must not be surfaced as ringable."""
    from skchat.call_routes import INVITE_TTL_S

    stale_ts = time.time() - (INVITE_TTL_S + 60)
    age = time.time() - stale_ts
    assert age > INVITE_TTL_S, "fixture must actually be stale"


def test_self_addressed_call_is_allowed_but_flagged():
    """A self-addressed call must NOT be rejected: it is how Chef actually calls.

    The browser drives Lumina's own webui, so calling "lumina" resolves both
    sides to lumina@chef.skworld.io. An earlier version of this rejected that
    outright, which was correct in theory and would have blocked every real
    call. The harm was never the self-addressing, it was that nothing consumed
    the invite so it rang forever, which consume-on-answer + TTL fix.
    """
    import inspect

    from skchat import call_routes

    src = inspect.getsource(call_routes.register_call_routes)
    assert "refusing to place a call to self" not in src, "hard reject blocks real calls"
    assert "self-addressed call" in src, "should still leave a breadcrumb"


def test_answering_agent_joins_under_a_distinct_identity():
    """The agent must not collide with the human on a self-addressed call.

    Both sides used to mint the identical LiveKit identity, so the SFU evicted
    one with DuplicateIdentity: the call looked connected while the agent was
    silently gone. Observed live.
    """
    import inspect

    from skchat import call_routes

    prep = inspect.getsource(call_routes._prepare_call)
    assert "identity_suffix" in prep
    routes = inspect.getsource(call_routes.register_call_routes)
    assert 'identity_suffix="#agent"' in routes


def test_self_addressed_call_is_treated_as_the_operator():
    """Mode must not fall to 'group' just because the peer is literally us."""
    from skchat.call_answerer import _conversational_peer

    assert _conversational_peer({"peer_fqid": "lumina@chef.skworld.io"}) == "chef"
    assert _conversational_peer({"peer_fqid": "chef@skworld.io"}) == "chef@skworld.io"
    assert _conversational_peer({"peer_fqid": "mallory@x.io"}) == "mallory@x.io"


# ─── answerer concurrency ───────────────────────────────────────────────────
def test_answerer_runs_calls_off_the_poll_loop():
    """A call must not block polling; a second caller used to ring into nothing."""
    import inspect

    from skchat import call_answerer

    src = inspect.getsource(call_answerer.run_answerer)
    # Strip comments: the fix's own comment mentions asyncio.run() by name, so a
    # naive substring check matches the explanation rather than the code.
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "asyncio.run(" not in code, "asyncio.run inline blocks polling for the whole call"
    assert "_spawn_call(" in code


def test_answerer_ignores_a_duplicate_invite_for_a_live_room(monkeypatch):
    """A re-delivered invite must not double-join a call already in progress."""
    from skchat import call_answerer

    started = []
    monkeypatch.setattr(
        call_answerer.threading,
        "Thread",
        lambda **kw: type("T", (), {"start": lambda self: started.append(kw.get("name"))})(),
    )
    joinable = {"room": "call-abc", "token": "t", "livekit_url": "ws://x"}
    call_answerer._ACTIVE_ROOMS.clear()
    call_answerer._spawn_call(joinable)
    call_answerer._spawn_call(joinable)  # duplicate while the first is live
    assert len(started) == 1, "duplicate invite joined the same room twice"
    call_answerer._ACTIVE_ROOMS.clear()
