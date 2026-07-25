"""Tests for the shared reply-model resolver wired into the two bridges.

scripts/lumina-bridge.py and scripts/opus-bridge.py used to resolve the reply
model with `skchat.agent_model.get_model(agent)`, a concrete-id-only lookup
that ignored a role selection (e.g. sk-creative) or a per-chat pin. They now
route through `skchat.reply_model.resolve_reply_backend`, the one resolver
every surface (app, Telegram, voice) shares, via a `_role_resolve` wrapper
over `skos.models.resolve(role=...)` that fails soft to (None, None) on any
error so a broken/missing skos install never breaks a reply.

Each bridge is loaded via importlib (hyphenated filenames), same pattern as
test_bridge_loop_guard.py.
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


@pytest.fixture(params=["lumina-bridge.py", "opus-bridge.py"])
def bridge(request, tmp_path, monkeypatch):
    # Keep loop-guard machinery out of the way; not exercised by these tests.
    monkeypatch.setenv("SKCHAT_PEERS_DIR", str(tmp_path / "peers-empty"))
    modname = request.param.replace("-", "_").replace(".py", "") + "_reply_model_test"
    return _load(request.param, modname)


# ── _openai_chat_url: pure normalization ────────────────────────────────────


class TestOpenaiChatUrl:
    def test_bare_v1_gets_chat_completions_appended(self, bridge):
        expected = "http://host:8082/v1/chat/completions"
        assert bridge._openai_chat_url("http://host:8082/v1") == expected

    def test_full_endpoint_is_left_alone(self, bridge):
        url = "http://host:18780/v1/chat/completions"
        assert bridge._openai_chat_url(url) == url

    def test_bare_host_gets_full_suffix(self, bridge):
        expected = "http://host:8082/v1/chat/completions"
        assert bridge._openai_chat_url("http://host:8082") == expected

    def test_trailing_slash_is_stripped_first(self, bridge):
        expected = "http://host:8082/v1/chat/completions"
        assert bridge._openai_chat_url("http://host:8082/v1/") == expected


# ── _role_resolve: fail-soft wrapper over skos.models.resolve ──────────────


class TestRoleResolve:
    def test_success_normalizes_url_and_returns_model(self, bridge, monkeypatch):
        class _FakeBackend:
            url = "http://skos-host:9/v1"
            model = "role-model"

        monkeypatch.setattr("skos.models.resolve", lambda role: _FakeBackend())

        url, model = bridge._role_resolve("sk-creative")
        assert url == "http://skos-host:9/v1/chat/completions"
        assert model == "role-model"

    def test_resolve_exception_fails_soft_to_none_none(self, bridge, monkeypatch):
        def _boom(role):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr("skos.models.resolve", _boom)

        assert bridge._role_resolve("sk-creative") == (None, None)

    def test_empty_backend_url_fails_soft_to_none_none(self, bridge, monkeypatch):
        class _EmptyBackend:
            url = ""
            model = ""

        monkeypatch.setattr("skos.models.resolve", lambda role: _EmptyBackend())

        assert bridge._role_resolve("sk-creative") == (None, None)

    def test_missing_skos_module_fails_soft_to_none_none(self, bridge, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "skos.models" or name.startswith("skos.models"):
                raise ImportError("skos.models not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)
        assert bridge._role_resolve("sk-creative") == (None, None)


# ── _skgateway_reply: full resolver wiring ──────────────────────────────────


class TestSkgatewayReplyResolverWiring:
    GW = "http://localhost:18780/v1/chat/completions"
    ROLE_URL = "http://skos-host/v1/chat/completions"

    def test_no_gateway_configured_returns_none(self, bridge, monkeypatch):
        monkeypatch.delenv("SKCHAT_LLM_URL", raising=False)
        assert bridge._skgateway_reply("sys", "hi") is None

    def test_concrete_selection_calls_gateway_with_the_concrete_id(self, bridge, monkeypatch):
        monkeypatch.setenv("SKCHAT_LLM_URL", self.GW)
        monkeypatch.setattr("skchat.agent_model.get_selection", lambda agent: "claude-opus-4-8")

        calls = []

        def _fake_call(url, model, system_prompt, message):
            calls.append((url, model))
            return "reply text"

        monkeypatch.setattr(bridge, "_skgateway_call", _fake_call)

        out = bridge._skgateway_reply("sys prompt", "hello")
        assert out == "reply text"
        assert calls == [(self.GW, "claude-opus-4-8")]

    def test_role_selection_routes_through_role_resolve(self, bridge, monkeypatch):
        monkeypatch.setenv("SKCHAT_LLM_URL", self.GW)
        monkeypatch.setattr("skchat.agent_model.get_selection", lambda agent: "sk-creative")
        monkeypatch.setattr(bridge, "_role_resolve", lambda role: (self.ROLE_URL, "role-model"))

        calls = []

        def _fake_call(url, model, system_prompt, message):
            calls.append((url, model))
            return "reply text"

        monkeypatch.setattr(bridge, "_skgateway_call", _fake_call)

        out = bridge._skgateway_reply("sys prompt", "hello")
        assert out == "reply text"
        assert calls == [(self.ROLE_URL, "role-model")]

    def test_resolver_failure_falls_back_to_gateway_default(self, bridge, monkeypatch):
        monkeypatch.setenv("SKCHAT_LLM_URL", self.GW)

        def _boom(*args, **kwargs):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr("skchat.reply_model.resolve_reply_backend", _boom)

        calls = []

        def _fake_call(url, model, system_prompt, message):
            calls.append((url, model))
            return "reply text"

        monkeypatch.setattr(bridge, "_skgateway_call", _fake_call)

        out = bridge._skgateway_reply("sys prompt", "hello")
        assert out == "reply text"
        assert calls == [(self.GW, "ornith-tiny")]

    def test_primary_failure_falls_back_on_gateway_url(self, bridge, monkeypatch):
        # Role-resolved reply_url differs from the plain gateway url; the hard
        # fallback retry must still go through the plain gateway url (SKCHAT_LLM_URL),
        # matching the pre-existing fallback contract.
        monkeypatch.setenv("SKCHAT_LLM_URL", self.GW)
        monkeypatch.setattr("skchat.agent_model.get_selection", lambda agent: "sk-creative")
        monkeypatch.setattr(bridge, "_role_resolve", lambda role: (self.ROLE_URL, "role-model"))

        calls = []

        def _fake_call(url, model, system_prompt, message):
            calls.append((url, model))
            if model == "role-model":
                return None  # primary call fails
            return "fallback reply"

        monkeypatch.setattr(bridge, "_skgateway_call", _fake_call)

        out = bridge._skgateway_reply("sys prompt", "hello")
        assert out == "fallback reply"
        assert calls == [
            (self.ROLE_URL, "role-model"),
            (self.GW, "ornith-tiny"),
        ]

    def test_custom_fallback_model_env_is_honored(self, bridge, monkeypatch):
        monkeypatch.setenv("SKCHAT_LLM_URL", self.GW)
        monkeypatch.setenv("SKCHAT_LLM_FALLBACK_MODEL", "custom-fallback")
        monkeypatch.setattr("skchat.agent_model.get_selection", lambda agent: "claude-opus-4-8")

        calls = []

        def _fake_call(url, model, system_prompt, message):
            calls.append((url, model))
            return None if model == "claude-opus-4-8" else "fallback reply"

        monkeypatch.setattr(bridge, "_skgateway_call", _fake_call)

        out = bridge._skgateway_reply("sys prompt", "hello")
        assert out == "fallback reply"
        assert calls == [(self.GW, "claude-opus-4-8"), (self.GW, "custom-fallback")]

    def test_both_calls_fail_returns_none(self, bridge, monkeypatch):
        monkeypatch.setenv("SKCHAT_LLM_URL", self.GW)
        monkeypatch.setattr("skchat.agent_model.get_selection", lambda agent: "claude-opus-4-8")
        monkeypatch.setattr(bridge, "_skgateway_call", lambda *a, **kw: None)

        assert bridge._skgateway_reply("sys prompt", "hello") is None
