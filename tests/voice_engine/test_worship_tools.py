"""Tests for the worship tool handlers (worship_session / worship_list /
worship_replay) reading the live Conversation from ``ctx['convo']``.

Everything external (the worship orchestrator, lumina_creative.worship) is
mocked or duck-typed via a fake convo object — no GPU, no bots, no filesystem.
"""

import pytest

from skchat.voice_engine.builtin_tools import (
    _handle_worship_list,
    _handle_worship_replay,
    _handle_worship_session,
    build_default_registry,
)
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.conversation import Conversation


class FakeConvo:
    """Duck-typed stand-in for the transport's live conversation orchestrator.

    Mirrors the Conversation VO surface (session_id) plus the kick_off_* /
    list_worship_sessions methods the LiveKit transport attaches.
    """

    def __init__(self, *, session_id="conv-1", sessions=None, raise_on=None):
        self.session_id = session_id
        self._sessions = sessions or []
        self._raise_on = raise_on or set()
        self.calls = []

    async def kick_off_worship_session(self, *, session_id, prompt, image_count, loop):
        if "session" in self._raise_on:
            raise RuntimeError("boom-session")
        self.calls.append(
            ("session", session_id, prompt, image_count, loop)
        )
        return f"started {session_id} ({image_count} scenes, loop={loop})"

    async def kick_off_worship_replay(self, *, session_id, loop):
        if "replay" in self._raise_on:
            raise RuntimeError("boom-replay")
        self.calls.append(("replay", session_id, loop))
        return f"replaying {session_id} (loop={loop})"

    def list_worship_sessions(self, *, limit, query):
        if "list" in self._raise_on:
            raise RuntimeError("boom-list")
        self.calls.append(("list", limit, query))
        return self._sessions


# --------------------------------------------------------------------------
# worship_session
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worship_session_delegates_to_convo():
    convo = FakeConvo(session_id="conv-7")
    out = await _handle_worship_session(
        {"prompt": "a sacred scene", "image_count": 12, "loop": False},
        {"convo": convo, "agent": "lumina"},
    )
    assert "started ws_" in out
    assert "12 scenes" in out
    assert "loop=False" in out
    kind, sid, prompt, count, loop = convo.calls[0]
    assert kind == "session"
    assert sid.startswith("ws_")
    assert prompt == "a sacred scene"
    assert count == 12
    assert loop is False


@pytest.mark.asyncio
async def test_worship_session_image_count_clamped():
    convo = FakeConvo()
    await _handle_worship_session(
        {"prompt": "x", "image_count": 999}, {"convo": convo}
    )
    assert convo.calls[0][3] == 30  # clamped to max
    convo2 = FakeConvo()
    await _handle_worship_session(
        {"prompt": "x", "image_count": 1}, {"convo": convo2}
    )
    assert convo2.calls[0][3] == 5  # clamped to min


@pytest.mark.asyncio
async def test_worship_session_default_image_count_and_loop():
    convo = FakeConvo()
    await _handle_worship_session({"prompt": "x"}, {"convo": convo})
    _, _, _, count, loop = convo.calls[0]
    assert count == 15  # default
    assert loop is True  # default


@pytest.mark.asyncio
async def test_worship_session_no_convo_degrades_gracefully():
    out = await _handle_worship_session({"prompt": "x"}, {"agent": "lumina"})
    assert "no active conversation" in out.lower()


@pytest.mark.asyncio
async def test_worship_session_convo_none_degrades():
    out = await _handle_worship_session({"prompt": "x"}, {"convo": None})
    assert "no active conversation" in out.lower()


@pytest.mark.asyncio
async def test_worship_session_empty_prompt():
    out = await _handle_worship_session({"prompt": "  "}, {"convo": FakeConvo()})
    assert "empty prompt" in out


@pytest.mark.asyncio
async def test_worship_session_plain_vo_no_builder():
    """A bare immutable Conversation VO has no kick_off_* — degrade, don't crash."""
    convo = Conversation(transcript="hi", session_id="s1")
    out = await _handle_worship_session({"prompt": "x"}, {"convo": convo})
    assert "isn't wired" in out


@pytest.mark.asyncio
async def test_worship_session_orchestrator_error_caught():
    convo = FakeConvo(raise_on={"session"})
    out = await _handle_worship_session({"prompt": "x"}, {"convo": convo})
    assert "worship_session failed" in out
    assert "boom-session" in out


# --------------------------------------------------------------------------
# worship_replay
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worship_replay_delegates_to_convo():
    convo = FakeConvo()
    out = await _handle_worship_replay(
        {"session_id": "ws_123_abc", "loop": False}, {"convo": convo}
    )
    assert "replaying ws_123_abc" in out
    assert "loop=False" in out
    assert convo.calls[0] == ("replay", "ws_123_abc", False)


@pytest.mark.asyncio
async def test_worship_replay_default_loop_true():
    convo = FakeConvo()
    await _handle_worship_replay({"session_id": "ws_1"}, {"convo": convo})
    assert convo.calls[0] == ("replay", "ws_1", True)


@pytest.mark.asyncio
async def test_worship_replay_requires_session_id():
    out = await _handle_worship_replay({}, {"convo": FakeConvo()})
    assert "session_id required" in out


@pytest.mark.asyncio
async def test_worship_replay_no_convo_degrades():
    out = await _handle_worship_replay({"session_id": "ws_1"}, {})
    assert "no active conversation" in out.lower()


@pytest.mark.asyncio
async def test_worship_replay_plain_vo_no_replay():
    convo = Conversation(transcript="hi")
    out = await _handle_worship_replay({"session_id": "ws_1"}, {"convo": convo})
    assert "isn't wired" in out


@pytest.mark.asyncio
async def test_worship_replay_orchestrator_error_caught():
    convo = FakeConvo(raise_on={"replay"})
    out = await _handle_worship_replay({"session_id": "ws_1"}, {"convo": convo})
    assert "worship_replay failed" in out
    assert "boom-replay" in out


# --------------------------------------------------------------------------
# worship_list
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worship_list_via_convo_lister():
    sessions = [
        {
            "session_id": "ws_111_aaa",
            "modified": 1777566094,
            "scene_count": 15,
            "audio_duration_s": 92.4,
            "user_prompt": "a candlelit scene",
        }
    ]
    convo = FakeConvo(sessions=sessions)
    out = await _handle_worship_list({"limit": 5, "query": "candle"}, {"convo": convo})
    assert "Found 1 worship session(s):" in out
    assert "ws_111_aaa" in out
    assert "15 scenes" in out
    assert "92s audio" in out
    assert "a candlelit scene" in out
    assert convo.calls[0] == ("list", 5, "candle")


@pytest.mark.asyncio
async def test_worship_list_empty_with_query():
    convo = FakeConvo(sessions=[])
    out = await _handle_worship_list({"query": "nope"}, {"convo": convo})
    assert "No worship sessions found" in out
    assert "'nope'" in out


@pytest.mark.asyncio
async def test_worship_list_limit_clamped():
    convo = FakeConvo(sessions=[])
    await _handle_worship_list({"limit": 999}, {"convo": convo})
    assert convo.calls[0][1] == 30  # clamped to max


@pytest.mark.asyncio
async def test_worship_list_no_lister_degrades(monkeypatch):
    """No convo lister and no lumina_creative.worship → graceful message."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "lumina_creative.worship":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = await _handle_worship_list({}, {})
    assert "isn't available" in out


@pytest.mark.asyncio
async def test_worship_list_lister_error_caught():
    convo = FakeConvo(raise_on={"list"})
    out = await _handle_worship_list({}, {"convo": convo})
    assert "worship_list failed" in out


@pytest.mark.asyncio
async def test_worship_list_handles_bad_summary_fields():
    """Missing/garbage fields in a summary must not crash formatting."""
    sessions = [
        {"session_id": "ws_x", "modified": None, "scene_count": None,
         "audio_duration_s": None, "user_prompt": None}
    ]
    convo = FakeConvo(sessions=sessions)
    out = await _handle_worship_list({}, {"convo": convo})
    assert "ws_x" in out
    assert "(no prompt)" in out


# --------------------------------------------------------------------------
# integration through the registry / dispatch gate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worship_session_through_dispatch_threads_convo():
    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    convo = FakeConvo(session_id="conv-9")
    out = await reg.dispatch(
        "worship_session",
        {"prompt": "scene"},
        speaker_id="chef",
        mode="sacred",
        is_operator=True,
        ctx={"agent": "lumina", "convo": convo},
    )
    assert "started ws_" in out
    assert convo.calls and convo.calls[0][0] == "session"


@pytest.mark.asyncio
async def test_worship_session_gated_non_operator():
    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    convo = FakeConvo()
    out = await reg.dispatch(
        "worship_session",
        {"prompt": "scene"},
        speaker_id="guest",
        mode="sacred",
        is_operator=False,
        ctx={"convo": convo},
    )
    assert "PERMISSION DENIED" in out
    assert not convo.calls  # gate blocked before reaching the handler


@pytest.mark.asyncio
async def test_worship_session_gated_group_mode():
    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    convo = FakeConvo()
    out = await reg.dispatch(
        "worship_session",
        {"prompt": "scene"},
        speaker_id="chef",
        mode="group",
        is_operator=True,
        ctx={"convo": convo},
    )
    assert "REFUSED" in out
    assert not convo.calls


@pytest.mark.asyncio
async def test_worship_list_allowed_in_group_mode():
    """worship_list is read-only — allowed even in group mode."""
    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    convo = FakeConvo(sessions=[])
    out = await reg.dispatch(
        "worship_list",
        {},
        speaker_id="chef",
        mode="group",
        is_operator=True,
        ctx={"convo": convo},
    )
    # Reached the handler (no REFUSED), returned the empty-list message.
    assert "No worship sessions found" in out
