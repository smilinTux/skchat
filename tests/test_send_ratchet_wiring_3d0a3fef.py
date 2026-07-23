"""Card 3d0a3fef — CLI/webui/agent plaintext-downgrade regression.

The daemon builds its transport via ``ChatTransport.from_config`` (which wires the
agent's ChatCrypto so the RFC-0001 P1 DM ratchet can seal ``pqdr1`` frames), but the
CLI ``skchat send`` path, the webui, agent_comm, skseal and the group fan-out all
built ``ChatTransport(...)`` with the bare constructor: ``crypto=None`` leaves the
ratchet inert, so a send to a live-ratchet federated peer silently went out
PLAINTEXT (harvest-now-decrypt-later exposure).

These tests drive each surface's real transport builder with the ratchet enabled and
a live-ratchet peer, and assert the bytes handed to skcomms carry a ``pqdr1`` frame
and NOT the plaintext. Against the old raw-constructor code every one of them fails
(plaintext on the wire); with the ``from_config`` routing they pass.

Pure unit tests: SKComms, DmRatchetManager and ChatCrypto are all injected — no
network, no PQ backend, no real key material.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skchat.models import ChatMessage

PLAINTEXT = "harvest-me-later top secret 3d0a3fef"
# A name no real/local peer store can resolve: keeps the send off the loopback and
# federation branches so the sealed body lands on the plain skcomms.send wire.
PEER = "capauth:pqpeer-3d0a3fef@offnode.test"
IDENTITY = "capauth:alice@skworld.io"


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Enable the DM ratchet and stub every collaborator; return the mock comm.

    * ``SKComms.from_config`` (both ``skcomms`` and ``skcomms.core`` import styles
      resolve to the same class) returns a MagicMock whose ``.send`` records the
      wire payload.
    * ``load_agent_crypto`` returns a fake ChatCrypto so ``from_config`` wiring is
      observable; the bare constructor never calls it and stays ``crypto=None``.
    * ``DmRatchetManager.for_agent`` returns a manager with a LIVE session for
      ``PEER`` whose ``seal`` produces a ``pqdr1:`` frame.
    """
    monkeypatch.setenv("SKCHAT_DM_RATCHET", "1")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv("SKCOMMS_CONSENT_MODE", raising=False)

    comm = MagicMock()
    comm.router.transports = [MagicMock()]
    comm.send.return_value = MagicMock(delivered=True, successful_transport="file")
    comm.receive.return_value = []

    from skcomms.core import SKComms

    monkeypatch.setattr(SKComms, "from_config", staticmethod(lambda *a, **k: comm))

    crypto = MagicMock()
    crypto.can_sign = True
    crypto.is_ratchet_message.side_effect = lambda m: str(
        getattr(m, "content", "")
    ).startswith("pqdr1:")
    monkeypatch.setattr("skchat.crypto.load_agent_crypto", lambda identity=None: crypto)

    mgr = MagicMock()
    mgr.can_ratchet.side_effect = lambda peer: peer == PEER
    mgr.seal.side_effect = lambda m: m.model_copy(
        update={"content": f"pqdr1:{m.id}", "encrypted": True}
    )

    from skchat.dm_manager import DmRatchetManager

    monkeypatch.setattr(
        DmRatchetManager, "for_agent", staticmethod(lambda *a, **k: mgr)
    )
    return comm


def _msg() -> ChatMessage:
    return ChatMessage(sender=IDENTITY, recipient=PEER, content=PLAINTEXT)


def _assert_pqdr1_on_wire(comm) -> None:
    comm.send.assert_called_once()
    sent = comm.send.call_args.kwargs["message"]
    assert PLAINTEXT not in sent, "plaintext downgrade: DM body left the node unsealed"
    assert "pqdr1:" in sent, "no pqdr1 ratchet frame on the wire"


def test_cli_send_transport_seals_pqdr1(monkeypatch, wire):
    """`skchat send` (cli._get_transport) must ratchet a live-ratchet peer."""
    from skchat import cli

    monkeypatch.setattr(cli, "_get_history", lambda: MagicMock())
    monkeypatch.setattr(cli, "_get_identity", lambda: IDENTITY)

    transport = cli._get_transport()
    assert transport is not None
    assert transport.send_message(_msg())["delivered"] is True
    _assert_pqdr1_on_wire(wire)


def test_cli_chat_transport_seals_pqdr1(monkeypatch, wire):
    """cli._get_chat_transport (typing/notify path) must also carry the ratchet."""
    from skchat import cli

    monkeypatch.setattr(cli, "_get_history", lambda: MagicMock())
    monkeypatch.setattr(cli, "_get_identity", lambda: IDENTITY)

    transport = cli._get_chat_transport()
    assert transport is not None
    transport.send_message(_msg())
    _assert_pqdr1_on_wire(wire)


def test_webui_transport_seals_pqdr1(monkeypatch, wire):
    """webui /send transport must ratchet a live-ratchet peer."""
    from skchat import webui

    monkeypatch.setattr(webui, "_get_history", lambda: MagicMock())

    transport = webui._get_transport(IDENTITY)
    assert transport is not None
    transport.send_message(_msg())
    _assert_pqdr1_on_wire(wire)


def test_agent_comm_transport_seals_pqdr1(wire):
    """AgentMessenger's transport must ratchet a live-ratchet peer."""
    from skchat.agent_comm import AgentMessenger

    transport = AgentMessenger._try_init_transport(MagicMock(), IDENTITY, skcomms=wire)
    assert transport is not None
    transport.send_message(_msg())
    _assert_pqdr1_on_wire(wire)


def test_group_delivery_transport_seals_pqdr1(monkeypatch, wire):
    """Group fan-out member delivery must ratchet a live-ratchet peer."""
    import skchat.history
    from skchat import daemon_proxy_groups

    monkeypatch.setattr(skchat.history, "ChatHistory", lambda *a, **k: MagicMock())

    transport = daemon_proxy_groups._delivery_transport(IDENTITY)
    assert transport is not None
    transport.send_message(_msg())
    _assert_pqdr1_on_wire(wire)
