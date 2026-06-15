"""Tests for the agent-loop prevention guard in both bridges.

The bridges (scripts/lumina-bridge.py, scripts/opus-bridge.py) used to
LLM-auto-reply to EVERY sender — including other AI agents — which caused an
unbounded message storm (agent-A replies to agent-B replies to agent-A …).
The guard `_should_auto_reply(sender, content)` now refuses to reply to:
  - other AI agents (peer-store entity_type == "ai-agent" or known-agent name)
  - the bridge's own identity
  - envelope / context-dump garbage content

Each bridge is loaded via importlib (hyphenated filenames). We force the
peer-store lookup to miss (empty SKCHAT_PEERS_DIR) so the known-agent fallback
path is exercised deterministically.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def lumina(tmp_path, monkeypatch):
    # Point the peer store at an empty dir so get_peer() misses → fallback set.
    monkeypatch.setenv("SKCHAT_PEERS_DIR", str(tmp_path / "peers-empty"))
    monkeypatch.delenv("SKCHAT_BRIDGE_REPLY_TO_AGENTS", raising=False)
    return _load("lumina-bridge.py", "lumina_bridge_guard_test")


@pytest.fixture()
def opus(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_PEERS_DIR", str(tmp_path / "peers-empty"))
    monkeypatch.delenv("SKCHAT_BRIDGE_REPLY_TO_AGENTS", raising=False)
    return _load("opus-bridge.py", "opus_bridge_guard_test")


# ── shared parametrized behaviour ───────────────────────────────────────────

@pytest.fixture(params=["lumina", "opus"])
def bridge(request):
    return request.getfixturevalue(request.param)


class TestShouldAutoReply:
    def test_human_chef_full_uri_replies(self, bridge):
        assert bridge._should_auto_reply("capauth:chef@skworld.io", "hi there") is True

    def test_human_chef_short_replies(self, bridge):
        assert bridge._should_auto_reply("chef@skworld.io", "hello") is True

    def test_unknown_human_replies(self, bridge):
        assert bridge._should_auto_reply("randomperson@example.com", "hey") is True

    def test_known_agent_architect_skipped(self, bridge):
        assert bridge._should_auto_reply("capauth:architect@skworld.io", "ping") is False

    def test_known_agent_jarvis_skipped(self, bridge):
        assert bridge._should_auto_reply("jarvis@skworld.io", "status?") is False

    def test_known_agent_bare_name_skipped(self, bridge):
        assert bridge._should_auto_reply("opus", "yo") is False

    def test_json_envelope_content_skipped(self, bridge):
        envelope = '{"id": "abc", "sender": "x", "recipient": "y", "content": "hi"}'
        # even from a human sender, garbage envelope content is dropped
        assert bridge._should_auto_reply("capauth:chef@skworld.io", envelope) is False

    def test_context_dump_content_skipped(self, bridge):
        dump = "Chat context (recent):\n[opus] hello\n[lumina] hi"
        assert bridge._should_auto_reply("capauth:chef@skworld.io", dump) is False

    def test_empty_content_from_human_replies(self, bridge):
        # empty content is not envelope garbage; let normal pipeline handle it
        assert bridge._should_auto_reply("chef@skworld.io", "") is True

    def test_reply_to_agents_env_reenables(self, tmp_path, monkeypatch, request):
        monkeypatch.setenv("SKCHAT_PEERS_DIR", str(tmp_path / "peers-empty"))
        monkeypatch.setenv("SKCHAT_BRIDGE_REPLY_TO_AGENTS", "1")
        # reload fresh so module-level REPLY_TO_AGENTS picks up the env
        mod = _load("lumina-bridge.py", "lumina_bridge_reenable_test")
        assert mod.REPLY_TO_AGENTS is True
        assert mod._should_auto_reply("capauth:architect@skworld.io", "ping") is True
        # envelope content is STILL dropped even with a2a enabled
        env = '{"id":"x","sender":"a","recipient":"b"}'
        assert mod._should_auto_reply("capauth:architect@skworld.io", env) is False


class TestSelfSkip:
    def test_lumina_skips_self(self, lumina):
        assert lumina._should_auto_reply("capauth:lumina@skworld.io", "echo") is False

    def test_opus_skips_self(self, opus):
        assert opus._should_auto_reply("capauth:opus@skworld.io", "echo") is False


class TestHelpers:
    def test_local_part_extraction(self, bridge):
        assert bridge._sender_local_part("capauth:architect@skworld.io") == "architect"
        assert bridge._sender_local_part("lumina@skworld.io") == "lumina"
        assert bridge._sender_local_part("opus") == "opus"
        assert bridge._sender_local_part("") == ""

    def test_is_envelope_content(self, bridge):
        assert bridge._is_envelope_content('{"id": "1", "x": 2}') is True
        assert bridge._is_envelope_content("Chat context (recent):\nfoo") is True
        assert bridge._is_envelope_content('{"sender":"a","recipient":"b"}') is True
        assert bridge._is_envelope_content("just a normal message") is False
        assert bridge._is_envelope_content("") is False
