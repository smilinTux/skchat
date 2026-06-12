"""C — layered fallback: P2P first, LiveKit SFU on failure (same deterministic room)."""
import asyncio

import pytest

import skchat.call_orchestrator as co


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    monkeypatch.setattr(co, "_resolve", lambda peer: "lumina@chef.skworld")


def test_uses_p2p_when_it_opens(monkeypatch):
    async def _ok(fqid, timeout):
        return object()  # session opened
    monkeypatch.setattr(co, "_attempt_p2p", _ok)
    # fallback should NOT be called
    monkeypatch.setattr(co, "_livekit_fallback", lambda fqid: pytest.fail("should not fall back"))
    out = asyncio.run(co.connect_with_fallback("lumina"))
    assert out["transport"] == "p2p"
    assert out["peer_fqid"] == "lumina@chef.skworld"


def test_falls_back_to_livekit_on_p2p_timeout(monkeypatch):
    async def _timeout(fqid, timeout):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(co, "_attempt_p2p", _timeout)
    monkeypatch.setattr(
        co, "_livekit_fallback",
        lambda fqid: {"room": "call-xyz", "token": "tok", "livekit_url": "wss://x:8443",
                      "peer_fqid": fqid, "identity": "opus@chef.skworld"},
    )
    out = asyncio.run(co.connect_with_fallback("lumina", p2p_timeout=0.1))
    assert out["transport"] == "livekit"
    assert out["status"] == "fallback"
    assert out["room"] == "call-xyz"
    assert out["peer_fqid"] == "lumina@chef.skworld"


def test_falls_back_on_p2p_error(monkeypatch):
    async def _boom(fqid, timeout):
        raise RuntimeError("no ICE path")
    monkeypatch.setattr(co, "_attempt_p2p", _boom)
    monkeypatch.setattr(
        co, "_livekit_fallback",
        lambda fqid: {"room": "call-abc", "token": "t", "livekit_url": "u",
                      "peer_fqid": fqid, "identity": "opus@chef.skworld"},
    )
    out = asyncio.run(co.connect_with_fallback("lumina"))
    assert out["transport"] == "livekit"
    assert out["room"] == "call-abc"
