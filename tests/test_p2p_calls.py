"""skchat P2P glue: resolve + dial + status delegate to the session manager."""

import asyncio

import pytest

import skchat.p2p_calls as pc


@pytest.fixture(autouse=True)
def _reset():
    pc._manager = None
    pc._incoming.clear()
    yield
    pc._manager = None
    pc._incoming.clear()


def test_resolve_fqid_paired_and_barename(monkeypatch):
    monkeypatch.setattr(pc, "_list_peers", lambda: {"lumina@chef.skworld": {}})
    assert pc.resolve_fqid("lumina@chef.skworld") == "lumina@chef.skworld"
    assert pc.resolve_fqid("lumina") == "lumina@chef.skworld"


def test_resolve_fqid_rejects_unpaired_and_ambiguous(monkeypatch):
    monkeypatch.setattr(
        pc, "_list_peers", lambda: {"lumina@chef.skworld": {}, "lumina@other.world": {}}
    )
    with pytest.raises(ValueError):
        pc.resolve_fqid("nobody")
    with pytest.raises(ValueError):
        pc.resolve_fqid("lumina")  # ambiguous bare name


class _StubManager:
    def __init__(self):
        self.calls = []
        self.started = False
        self._sessions = {}

    async def call(self, fqid):
        self.calls.append(fqid)
        self._sessions[fqid] = type("S", (), {"is_open": True})()
        return self._sessions[fqid]

    async def start(self):
        self.started = True

    def active(self):
        return list(self._sessions)

    def get(self, peer):
        return self._sessions.get(peer)


def test_p2p_call_resolves_and_delegates(monkeypatch):
    monkeypatch.setattr(pc, "_list_peers", lambda: {"lumina@chef.skworld": {}})
    pc._manager = _StubManager()
    out = asyncio.run(pc.p2p_call("lumina"))
    assert out["peer_fqid"] == "lumina@chef.skworld"
    assert out["transport"] == "p2p"
    assert pc._manager.calls == ["lumina@chef.skworld"]


def test_p2p_listen_and_status(monkeypatch):
    monkeypatch.setattr(pc, "_list_peers", lambda: {"lumina@chef.skworld": {}})
    pc._manager = _StubManager()
    assert asyncio.run(pc.p2p_listen())["listening"] is True
    assert pc._manager.started is True
    asyncio.run(pc.p2p_call("lumina"))
    st = pc.p2p_status()
    assert st["active"] == [{"peer": "lumina@chef.skworld", "open": True}]
