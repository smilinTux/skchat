"""skchat ChatTransport → skcomms federation wiring.

When a recipient resolves to a reachable federation peer (https-s2s inbox_url),
send_message routes via skcomms.send_federated (the canonical signed S2S path);
otherwise it falls back to the legacy local transports.
"""

from __future__ import annotations

import pytest

from skchat.history import ChatHistory
from skchat.models import ChatMessage
from skchat.transport import ChatTransport


class FakeReport:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.successful_transport = "https-s2s" if delivered else None


class FakeSkcomms:
    def __init__(self, fed_delivered=True):
        self.federated_calls = []
        self.legacy_calls = []
        self._fed_delivered = fed_delivered

    def send_federated(self, to_fqid, message, **kw):
        self.federated_calls.append((to_fqid, message, kw))
        return FakeReport(self._fed_delivered)

    def send(self, recipient, message, **kw):
        self.legacy_calls.append((recipient, message, kw))
        return FakeReport(True)


@pytest.fixture
def tx(tmp_path):
    return ChatTransport(skcomms=FakeSkcomms(), history=ChatHistory(history_dir=tmp_path / "h"),
                         identity="capauth:jarvis@skworld.io")


def _msg(recipient="lumina"):
    return ChatMessage(sender="jarvis", recipient=recipient, content="hello")


def test_uses_federation_when_peer_resolves(tx, monkeypatch):
    monkeypatch.setattr(tx, "_federation_target", lambda r: "lumina@chef.skworld")
    res = tx.send_message(_msg("lumina"))
    assert res["delivered"] is True
    assert res["transport"] == "skfed-s2s"
    assert tx._skcomms.federated_calls and tx._skcomms.federated_calls[0][0] == "lumina@chef.skworld"
    assert not tx._skcomms.legacy_calls          # legacy path NOT used


def test_falls_back_to_legacy_when_not_federation(tx, monkeypatch):
    monkeypatch.setattr(tx, "_federation_target", lambda r: None)
    res = tx.send_message(_msg("somebody-local"))
    assert tx._skcomms.legacy_calls              # legacy path used
    assert not tx._skcomms.federated_calls


def test_falls_back_when_federation_undelivered(tmp_path, monkeypatch):
    sk = FakeSkcomms(fed_delivered=False)
    tx = ChatTransport(skcomms=sk, history=ChatHistory(history_dir=tmp_path / "h"),
                       identity="capauth:jarvis@skworld.io")
    monkeypatch.setattr(tx, "_federation_target", lambda r: "lumina@chef.skworld")
    tx.send_message(_msg("lumina"))
    assert sk.federated_calls and sk.legacy_calls   # tried fed, fell back to legacy


def test_federation_target_resolves_https_s2s_peer(tx, monkeypatch):
    class FakePeer:
        name = "lumina"; fqid = "lumina@chef.skworld"
        def inbox_url(self): return "http://100.108.59.57:9384/api/v1/inbox"
    class FakeStore:
        def list_all(self): return [FakePeer()]
    import skcomms.discovery as disc
    monkeypatch.setattr(disc, "PeerStore", lambda *a, **k: FakeStore())
    assert tx._federation_target("lumina") == "lumina@chef.skworld"
    assert tx._federation_target("capauth:lumina@skworld.io") == "lumina@chef.skworld"
    assert tx._federation_target("lumina@chef.skworld") == "lumina@chef.skworld"
    assert tx._federation_target("nobody") is None
