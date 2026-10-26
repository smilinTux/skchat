"""Text chat loads persona + memory context ONCE per session, never per message.

Chef's rule (2026-10-26). Covers skchat/session_context.py and the three text
paths that use it: GroupResponder (group + DM), LuminaBrain (the app DM brain
behind daemon_proxy) and the Telegram bridge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skchat.group_responder import GroupResponder, load_group_config
from skchat.models import ChatMessage
from skchat.session_context import SessionContext, TextContextConfig

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

MEM_BLOCK = "What you remember: Chef likes teal."


class _Loader:
    def __init__(self, block=MEM_BLOCK):
        self.block, self.calls = block, 0

    def __call__(self, agent, budget):
        self.calls += 1
        return self.block, {"total_chars": len(self.block)}


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --------------------------------------------------------------------------- #
# SessionContext
# --------------------------------------------------------------------------- #
def test_config_defaults_and_env():
    d = TextContextConfig.from_env({})
    assert d.enabled and not d.group and not d.per_message_recall
    assert d.budget == 3000 and d.idle_hours == 6.0
    e = TextContextConfig.from_env(
        {
            "SKCHAT_CONTEXT": "0",
            "SKCHAT_CONTEXT_BUDGET": "500",
            "SKCHAT_CONTEXT_GROUP": "1",
            "SKCHAT_CONTEXT_IDLE_HOURS": "0.5",
            "SKCHAT_PER_MESSAGE_RECALL": "1",
        }
    )
    assert not e.enabled and e.group and e.per_message_recall
    assert e.budget == 500 and e.idle_hours == 0.5


def test_built_once_per_session_and_stable():
    builds = []
    loader = _Loader()
    ctx = SessionContext(
        "lumina",
        lambda peer: builds.append(peer) or f"persona for {peer}",
        cfg=TextContextConfig(),
        context_loader=loader,
    )
    a = ctx.system_prompt("dm:chef", peer="chef", private=True)
    b = ctx.system_prompt("dm:chef", peer="chef", private=True)
    assert a == b and MEM_BLOCK in a
    assert builds == ["chef"] and loader.calls == 1
    # a different conversation is its own session
    ctx.system_prompt("dm:dave", peer="dave", private=True)
    assert ctx.persona_builds == 2 and loader.calls == 2


def test_idle_expiry_and_refresh_start_a_new_session():
    clock = _Clock()
    loader = _Loader()
    ctx = SessionContext(
        "lumina",
        lambda p: "persona",
        cfg=TextContextConfig(idle_hours=1),
        context_loader=loader,
        clock=clock,
    )
    ctx.system_prompt("k")
    clock.t += 1800  # 30 min: same session (and it slides the idle window)
    ctx.system_prompt("k")
    clock.t += 1800
    ctx.system_prompt("k")
    assert ctx.persona_builds == 1
    clock.t += 3601  # idle past the boundary
    ctx.system_prompt("k")
    assert ctx.persona_builds == 2
    ctx.refresh("k")
    ctx.system_prompt("k")
    assert ctx.persona_builds == 3 and loader.calls == 3


def test_group_sessions_get_no_memory_block_by_default():
    loader = _Loader()
    ctx = SessionContext(
        "lumina", lambda p: "persona", cfg=TextContextConfig(), context_loader=loader
    )
    assert ctx.system_prompt("g", private=False) == "persona"
    assert loader.calls == 0
    ctx2 = SessionContext(
        "lumina", lambda p: "persona", cfg=TextContextConfig(group=True), context_loader=loader
    )
    assert MEM_BLOCK in ctx2.system_prompt("g", private=False)


def test_context_off_and_loader_failure_degrade_to_persona():
    loader = _Loader()
    off = SessionContext(
        "lumina", lambda p: "persona", cfg=TextContextConfig(enabled=False), context_loader=loader
    )
    assert off.system_prompt("k") == "persona" and loader.calls == 0

    def boom(agent, budget):
        raise RuntimeError("disk gone")

    bad = SessionContext(
        "lumina", lambda p: "persona", cfg=TextContextConfig(), context_loader=boom
    )
    assert bad.system_prompt("k") == "persona"


# --------------------------------------------------------------------------- #
# GroupResponder
# --------------------------------------------------------------------------- #
class _Store:
    def __init__(self):
        self.searches, self.snaps = 0, []

    def search(self, q, limit=5, **kw):
        self.searches += 1
        return []

    def snapshot(self, **kw):
        self.snaps.append(kw)


class _Builder:
    def __init__(self):
        self.builds = 0

    def build(self, peer_name=None):
        self.builds += 1
        return f"You are Lumina, talking with {peer_name}."


class _Resp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "hey"}}]}


class _Http:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return _Resp()


def _responder(cfg=None):
    builder, store, http, loader = _Builder(), _Store(), _Http(), _Loader()
    r = GroupResponder(
        load_group_config("lumina", env={}),
        prompt_builder=builder,
        http=http,
        store=store,
        context_cfg=cfg or TextContextConfig(),
        context_loader=loader,
    )
    return r, builder, store, http, loader


def _dm(text):
    return ChatMessage(sender="chef@skworld.io", recipient="lumina@skworld.io", content=text)


def _group(text):
    return ChatMessage(
        sender="chef@skworld.io", recipient="group:room1", content=text, thread_id="room1"
    )


def test_dm_turn_pair_loads_once_and_searches_nothing():
    r, builder, store, http, loader = _responder()
    r.respond_direct(_dm("hi"))
    r.respond_direct(_dm("how are you?"))
    assert builder.builds == 1 and loader.calls == 1
    assert store.searches == 0 and r.memory_searches == 0
    s1, s2 = (c["messages"][0]["content"] for c in http.calls)
    assert s1 == s2 and MEM_BLOCK in s1
    # the user turn carries only the message, no recalled-memory preamble
    assert (
        http.calls[1]["messages"][-1]["content"] == "Message from chef@skworld.io:\nhow are you?"
    )
    # interactions are still stored back
    assert len(store.snaps) == 2


def test_group_turn_pair_loads_once_without_memory_block():
    r, builder, store, http, loader = _responder()
    r.respond(_group("@lumina hi"))
    r.respond(_group("@lumina again"))
    assert builder.builds == 1 and loader.calls == 0 and store.searches == 0
    assert http.calls[0]["messages"][0] == http.calls[1]["messages"][0]


def test_per_message_recall_opt_in_restores_search():
    r, _, store, _, _ = _responder(TextContextConfig(per_message_recall=True))
    r.respond_direct(_dm("hi"))
    r.respond_direct(_dm("again"))
    assert store.searches == 2 and r.memory_searches == 2


def test_refresh_context_rebuilds_on_next_message():
    r, builder, _, _, loader = _responder()
    r.respond_direct(_dm("hi"))
    r.refresh_context()
    r.respond_direct(_dm("hi again"))
    assert builder.builds == 2 and loader.calls == 2


# --------------------------------------------------------------------------- #
# LuminaBrain (daemon_proxy's DM brain)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def brain(monkeypatch, tmp_path):
    monkeypatch.delenv("SKCHAT_PER_MESSAGE_RECALL", raising=False)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import bridge_consciousness as bc

    b = bc.LuminaBrain(home=tmp_path / "lumina", agent="Lumina")
    loader = _Loader()
    b.context._loader = loader
    builds = []
    monkeypatch.setattr(b, "_build_system_prompt", lambda: builds.append(1) or "SOUL")
    sent = []
    monkeypatch.setattr(b, "_call", lambda msgs: sent.append(msgs) or "ok")

    class _LiveMem:
        injects = 0
        stores = 0

        def inject(self, sender, content, router=None):
            _LiveMem.injects += 1
            return "Relevant memories: x"

        def store(self, *a):
            _LiveMem.stores += 1

    b._mem = _LiveMem()
    return b, builds, sent, loader


def test_lumina_brain_turn_pair_loads_once(brain):
    b, builds, sent, loader = brain
    b.reply("hi", history=[], sender="chef")
    b.reply("again", history=[], sender="chef")
    assert len(builds) == 1 and loader.calls == 1
    assert b._mem.injects == 0 and b.memory_searches == 0
    assert b._mem.stores == 2
    assert sent[0][0] == sent[1][0] and MEM_BLOCK in sent[0][0]["content"]


def test_lumina_brain_per_message_recall_opt_in(brain):
    b, _, sent, _ = brain
    b.per_message_recall = True
    b.reply("hi", sender="chef")
    assert b._mem.injects == 1 and "Relevant memories" in sent[0][0]["content"]


# --------------------------------------------------------------------------- #
# Telegram bridge
# --------------------------------------------------------------------------- #
@pytest.fixture()
def bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_OPUS_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("SKC_BRIDGE_LLM_URL", "http://example.invalid/v1/chat/completions")
    monkeypatch.setenv("SKC_BRIDGE_AGENT_HOME", str(tmp_path / "opus"))
    monkeypatch.delenv("SKCHAT_PER_MESSAGE_RECALL", raising=False)
    skcomms = REPO.parent / "skcomms" / "src"
    for p in (str(SCRIPTS), str(skcomms)):
        if p not in sys.path:
            sys.path.insert(0, p)
    sys.modules.pop("telegram_bridge", None)
    try:
        import telegram_bridge as tb
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"telegram_bridge import failed: {exc}")
    loader = _Loader()
    builds = []
    monkeypatch.setattr(tb, "_build_system_prompt", lambda: builds.append(1) or "SOUL")
    tb._CONTEXT._loader = loader
    tb._CONTEXT.refresh()
    sent = []
    monkeypatch.setattr(
        tb, "_run_tool_loop", lambda msgs, **kw: sent.append(list(msgs)) or ("ok", "m")
    )
    monkeypatch.setattr(tb, "_curate_turn_tools", lambda text: [])
    monkeypatch.setattr(tb, "_resolve_backend_for_chat", lambda key: ("http://x", "m"))

    class _LiveMem:
        injects = 0

        def inject(self, *a, **k):
            _LiveMem.injects += 1
            return ""

        def store(self, *a):
            pass

    monkeypatch.setattr(tb, "_MEM", _LiveMem())
    yield tb, builds, sent, loader
    sys.modules.pop("telegram_bridge", None)


def test_telegram_turn_pair_loads_once(bridge):
    tb, builds, sent, loader = bridge
    tb._llm("4242", "hi")
    tb._llm("4242", "again")
    assert len(builds) == 1 and loader.calls == 1
    assert tb._MEM.injects == 0 and tb._MEMORY_SEARCHES == 0
    s1, s2 = sent[0][0]["content"], sent[1][0]["content"]
    assert s1 == s2
    assert "OPERATING FACTS" in s1 and MEM_BLOCK in s1


def test_telegram_group_chat_has_no_memory_block(bridge):
    tb, _, sent, loader = bridge
    tb._llm("-1001", "hi")
    assert loader.calls == 0 and MEM_BLOCK not in sent[0][0]["content"]


def test_telegram_refresh_command(bridge):
    tb, builds, _, _ = bridge
    tb._llm("4242", "hi")
    assert tb._handle_refresh_command("4242", "/refresh") is not None
    assert tb._handle_refresh_command("4242", "/model") is None
    tb._llm("4242", "hi again")
    assert len(builds) == 2
