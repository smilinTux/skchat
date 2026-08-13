"""The runtime fact must name the model and nothing else.

A model cannot introspect its own weights, so without being told it invents a
plausible name. But the first version of this prompt also handed it the
endpoint and the fallback model, and it read the lot back on a live call:
"running on claude-haiku-4-5 at localhost, fallback to qwen3.6-27b-abliterated
if needed". That is infrastructure leaking into a conversation, and it also
broadcast a stale config value as though it were true.
"""

import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.engine import VoiceEngine


class _FakeLLM:
    def __init__(self):
        self.system = ""

    async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
        self.system = messages[0]["content"]
        return "ok"


class _FakeMem:
    async def search(self, q, agent, limit=3):
        return ""

    async def snapshot(self, *a, **k):
        return True


class _FakePersona:
    def build(self, agent, *, mode="sacred"):
        return f"You are {agent}."


@pytest.mark.asyncio
async def test_system_prompt_names_the_model_but_leaks_no_infrastructure():
    cfg = VoiceConfig.from_env(
        env={
            "SKVOICE_MODEL": "claude-haiku-4-5",
            "SKVOICE_LLM_URL": "http://localhost:18783/v1/chat/completions",
            "SKVOICE_FALLBACK_MODEL": "ornith-1.0-9b",
            "SKVOICE_FALLBACK_URL": "http://192.168.0.100:8082/v1/chat/completions",
        }
    )
    llm = _FakeLLM()
    eng = VoiceEngine(cfg, agent="lumina", llm=llm, memory=_FakeMem(), persona=_FakePersona())
    await eng.respond("what model are you", history=[], mode="sacred")

    assert "claude-haiku-4-5" in llm.system, "she must be told her own model name"
    # None of this belongs in something she might say out loud.
    for leak in ("18783", "localhost", "192.168.0.100", "8082", "ornith-1.0-9b"):
        assert leak not in llm.system, f"runtime fact leaked {leak!r} into the prompt"
