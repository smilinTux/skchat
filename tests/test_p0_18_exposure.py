"""SKMEM-P0-18 follow-up: unauthenticated surfaces never get owner context.

All stores/homes are tmp; nothing here touches a real agent.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from skchat.group_responder import GroupResponder, load_group_config
from skchat.models import ChatMessage
from skchat.session_context import TextContextConfig

WHISPER = "WHISPER-DIGEST: the whole store in one paragraph"


@pytest.fixture(autouse=True)
def _tmp_homes(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "skc"))
    monkeypatch.setenv("SKMEMORY_HOME", str(tmp_path / "skm"))
    monkeypatch.delenv("SKMEMORY_PG_DSN", raising=False)


class _Resp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


class _Http:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return _Resp()


class _Builder:
    def build(self, peer_name=None):
        return "You are Lumina."


class _Store:
    def search(self, q, limit=5, **kw):
        return []

    def snapshot(self, *a, **k):
        pass


_CFG = load_group_config("lumina", {"SKCHAT_GROUPS": "room1"})


def _responder(http, **ctx):
    return GroupResponder(
        _CFG,
        prompt_builder=_Builder(),
        http=http,
        store=_Store(),
        context_cfg=TextContextConfig(**ctx),
        context_loader=lambda agent, budget: (WHISPER, {}),
    )


@pytest.mark.parametrize("sender", ["mallory@example.org", "chef@skworld.io"])
@pytest.mark.parametrize("group_optin", [False, True])
def test_dm_prompt_has_no_whisper_or_journal_for_anyone(sender, group_optin):
    http = _Http()
    msg = ChatMessage(sender=sender, recipient="lumina", content="what do you know?")
    _responder(http, group=group_optin).respond_direct(msg)
    assert WHISPER not in json.dumps(http.calls[0]["messages"])


def test_group_prompt_has_no_whisper_by_default():
    http = _Http()
    msg = ChatMessage(
        sender="pat@skworld.io", recipient="group:room1", content="@lumina hi", thread_id="room1"
    )
    _responder(http).respond(msg)
    assert WHISPER not in json.dumps(http.calls[0]["messages"])


def test_voice_engine_demotes_non_operator_from_1to1_register():
    from skchat.voice_engine.config import VoiceConfig
    from skchat.voice_engine.engine import VoiceEngine

    seen = {}

    class LLM:
        async def reply(self, messages, **kw):
            seen["system"] = messages[0]["content"]
            return "x"

    class Persona:
        def build(self, agent, *, mode="sacred"):
            return f"persona-{mode}"

    class Mem:
        async def search(self, *a, **k):
            return ""

    def run(is_operator):
        eng = VoiceEngine(
            VoiceConfig.from_env(env={}),
            agent="lumina",
            llm=LLM(),
            memory=Mem(),
            persona=Persona(),
            registry=None,
            context_loader=lambda agent, budget: (WHISPER, {}),
        )
        asyncio.run(
            eng.respond("hello", [], mode="sacred", speaker_id="s", is_operator=is_operator)
        )
        return seen["system"]

    assert WHISPER in run(True)
    assert WHISPER not in run(False)


def test_websocket_is_never_operator_and_never_1to1(monkeypatch):
    from skchat.transports import websocket as wsmod

    got = {}

    class Engine:
        async def respond(self, text, history, **kw):
            got.update(kw)
            return "hi"

    class WS:
        async def send_json(self, *_):
            pass

        async def send_bytes(self, *_):
            pass

    async def no_tts(*a, **k):
        return None

    monkeypatch.setattr(wsmod, "_synthesize", no_tts)
    asyncio.run(
        wsmod._process_text(
            WS(), "hello", [], Engine(), "lumina", group_suffix=[], conn_id="c", pending_group={}
        )
    )
    assert got["is_operator"] is False
    assert got["mode"] == "group"
    assert got["speaker_id"] != "chef"


def test_voice_ws_binds_loopback_by_default(monkeypatch):
    from skchat.transports import serve_ws

    monkeypatch.delenv("SKCHAT_VOICE_HOST", raising=False)
    seen = {}
    monkeypatch.setattr("skchat.transports.websocket.build_app", lambda: object())
    with patch("uvicorn.run", lambda app, host, port, log_level: seen.update(host=host)):
        serve_ws.main()
    assert seen["host"] == "127.0.0.1"


def test_get_context_for_message_passes_public_reader_and_fails_closed():
    from skchat import mcp_server

    calls = []

    def fake_call(name, args):
        calls.append((name, args))
        return {"memories": [{"content": "pub fact"}]}

    with patch.object(mcp_server, "_call_skcapstone", fake_call):
        with patch.object(mcp_server, "_skcapstone_http_accepts_reader", lambda: True):
            res = asyncio.run(mcp_server._handle_get_context_for_message({"query": "q"}))
        assert json.loads(res[0].text)["count"] == 1
        assert calls[0][1]["reader"] == "@public"
        calls.clear()
        with patch.object(mcp_server, "_skcapstone_http_accepts_reader", lambda: False):
            res = asyncio.run(mcp_server._handle_get_context_for_message({"query": "q"}))
        assert json.loads(res[0].text)["count"] == 0 and not calls
