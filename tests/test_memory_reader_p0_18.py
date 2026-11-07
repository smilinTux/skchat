"""SKMEM-P0-18: group and unknown surfaces must not read owner-scope memory.

The first test uses a REAL skmemory MemoryStore (tmp home) holding a
``@chef-only`` memory and drives the group responder end to end: before the fix
the memory rode into the outbound group prompt.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import patch

import pytest

pytest.importorskip("skmemory")
from skmemory.models import Memory, MemoryLayer  # noqa: E402
from skmemory.store import MemoryStore  # noqa: E402

if "reader" not in inspect.signature(MemoryStore.search).parameters:  # pragma: no cover
    pytest.skip(
        "installed skmemory predates search(reader=); need >=0.11.94", allow_module_level=True
    )

from skchat import memory_reader  # noqa: E402
from skchat.group_responder import GroupResponder, load_group_config  # noqa: E402
from skchat.models import ChatMessage  # noqa: E402
from skchat.session_context import TextContextConfig  # noqa: E402

SECRET = "zebra-quartz private medical note"
PUBLIC_TXT = "zebra-quartz team standup is at nine"


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Never touch a live store: explicit tmp backend, no vector/graph, and the
    # developer-shell selectors / PG / embed settings are dropped.
    from skmemory.backends.sqlite_backend import SQLiteBackend

    for var in (
        "SKMEMORY_READER",
        "SKMEMORY_PG_DSN",
        "SKMEMORY_VECTOR_BACKEND",
        "SKMEMORY_EMBED_URL",
        "SKMEMORY_EMBED_URLS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SKMEMORY_EMBED_URL", "http://127.0.0.1:1/api/embed")
    base = tmp_path / "mem"
    base.mkdir()
    s = MemoryStore(primary=SQLiteBackend(base_path=str(base)), vector=None, graph=None)
    assert str(base) in str(getattr(s.primary, "base_path", base))
    for mid, text, tag in (("m-secret", SECRET, "@chef-only"), ("m-pub", PUBLIC_TXT, "@public")):
        s.primary.save(
            Memory(
                id=mid,
                title=text[:20],
                content=text,
                layer=MemoryLayer.LONG,
                tags=[],
                context_tag=tag,
            )
        )
    return s


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


_CFG = load_group_config("lumina", {"SKCHAT_GROUPS": "room1"})


def _responder(store, http):
    return GroupResponder(
        _CFG,
        prompt_builder=_Builder(),
        http=http,
        store=store,
        context_cfg=TextContextConfig(per_message_recall=True),
    )


def _outbound(http) -> str:
    return json.dumps(http.calls[0]["messages"])


def test_group_reply_cannot_carry_owner_scope_memory(store):
    http = _Http()
    msg = ChatMessage(
        sender="pat@skworld.io",
        recipient="group:room1",
        content="@lumina zebra-quartz",
        thread_id="room1",
    )
    _responder(store, http).respond(msg)
    out = _outbound(http)
    assert SECRET not in out
    assert PUBLIC_TXT in out


def test_dm_reads_as_public_until_chef_is_verified(store):
    http = _Http()
    msg = ChatMessage(sender="chef@skworld.io", recipient="lumina", content="zebra-quartz")
    _responder(store, http).respond_direct(msg)
    out = _outbound(http)
    assert SECRET not in out and PUBLIC_TXT in out


def test_owner_still_reads_everything(store):
    texts = [m.content for m in memory_reader.search_as(store, "zebra-quartz", 10, None)]
    assert SECRET in texts and PUBLIC_TXT in texts


def test_old_skmemory_without_reader_fails_closed():
    class OldStore:
        def search(self, query, limit=10):  # no reader kwarg
            raise AssertionError("must not fall back to an unfiltered search")

    assert memory_reader.search_as(OldStore(), "q", 5, "@public") == []
    assert memory_reader.search_as(OldStore(), "q", 5, "channel:g") == []


def test_reader_selection():
    assert memory_reader.reader_for_message("room1") == "channel:room1"
    assert memory_reader.reader_for_message(None, "group:g2") == "channel:group:g2"
    assert memory_reader.reader_for_message(None, "lumina") == "@public"
    assert memory_reader.voice_reader("sacred", True) is None
    assert memory_reader.voice_reader("private", True) is None
    assert memory_reader.voice_reader("group", True) == "@public"
    assert memory_reader.voice_reader("sacred", False) == "@public"
    assert memory_reader.voice_reader("sacred", None) == "@public"
    assert memory_reader.voice_reader("sacred", "yes") == "@public"


def test_voice_search_memory_tool_scopes_by_operator(store):
    from skchat.voice_engine.builtin_tools import _handle_search_memory

    def run(ctx):
        with patch("skmemory.MemoryStore", return_value=store):
            return asyncio.run(_handle_search_memory({"query": "zebra-quartz"}, ctx))

    assert SECRET in run({"mode": "sacred", "is_operator": True})
    assert SECRET not in run({"mode": "group", "is_operator": True})
    assert SECRET not in run({"mode": "sacred", "is_operator": False})
    assert SECRET not in run({})
    assert PUBLIC_TXT in run({})


def test_voice_bridge_defaults_to_public(store):
    from skchat.voice_engine.memory import MemoryBridge

    with patch("skmemory.MemoryStore", return_value=store):
        pub = asyncio.run(MemoryBridge().search("zebra-quartz", "lumina"))
        own = asyncio.run(MemoryBridge().search("zebra-quartz", "lumina", reader=None))
    assert SECRET not in pub and PUBLIC_TXT in pub
    assert SECRET in own


class _Run:
    """Fake stdio skcapstone-mcp: rejects any request sent before initialize."""

    def __init__(self, tools_has_reader, results):
        self.calls = []
        self.tools_has_reader = tools_has_reader
        self.results = results

    def __call__(self, cmd, input=None, **kw):
        out, ready = [], False
        for line in input.splitlines():
            req = json.loads(line)
            self.calls.append(req)
            m, rid = req["method"], req.get("id")
            if m == "initialize":
                ready = True
                out.append({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}})
            elif rid is None:
                continue
            elif not ready:
                out.append({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602}})
            elif m == "tools/list":
                props = {"query": {}, "reader": {}} if self.tools_has_reader else {"query": {}}
                tool = {"name": "memory_search", "inputSchema": {"properties": props}}
                out.append({"jsonrpc": "2.0", "id": rid, "result": {"tools": [tool]}})
            else:
                text = json.dumps(self.results)
                out.append(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"content": [{"type": "text", "text": text}]},
                    }
                )

        class R:
            returncode = 0
            stdout = "\n".join(json.dumps(o) for o in out)

        return R()


def test_advocacy_handshake_passes_reader_and_fails_closed_on_old_mcp():
    from skchat import advocacy

    new = _Run(True, [{"content": "pub fact"}])
    with patch("skchat.advocacy.subprocess.run", new):
        out = advocacy.AdvocacyEngine()._get_memory_context("q", reader="channel:room1")
    assert "pub fact" in out
    assert new.calls[0]["method"] == "initialize"
    call = [c for c in new.calls if c["method"] == "tools/call"][0]
    assert call["params"]["arguments"]["reader"] == "channel:room1"

    old = _Run(False, [{"content": "owner secret"}])
    with patch("skchat.advocacy.subprocess.run", old):
        assert advocacy.AdvocacyEngine()._get_memory_context("q") == ""


def test_context_default_source_reads_as_thread_channel():
    from skchat import context

    seen = []
    with patch(
        "skchat.advocacy.AdvocacyEngine._get_memory_context",
        lambda self, q, reader="@public": seen.append(reader) or "Relevant context:\n- x",
    ):
        context._default_memory_source("q", 3, reader="channel:t1")
        context._default_memory_source("q", 3)
    assert seen == ["channel:t1", "@public"]
