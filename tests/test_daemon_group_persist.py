"""Regression test for the group fan-out persistence gap (wave-a live-verify
item 3): a fanned-in GROUP message from a PEER AGENT (loop-breaker vetoes a
reply) must still be persisted to the shared group thread, so
GET /api/v1/conversations/<gid> (what the webui/human sees) shows it.

Before the fix, daemon.py's receive-loop group branch only wrote a canonical
``recipient == "group:<gid>"`` copy of the message when
``group_responder.respond()`` produced a further reply (which happens only
for messages this agent should itself answer). A peer agent's own reply,
fanned in via file-delivery, never @-mentions this agent, so
``should_respond()`` vetoes an answer and the incoming copy was silently
dropped — it only ever landed with ``recipient=<this agent's own URI>`` (the
fan-out shape), which ``daemon_proxy_groups.group_thread_messages`` filters
out by design (keeps only the canonical group-thread row per message).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from skchat.daemon import ChatDaemon
from skchat.models import ChatMessage


def _peer_group_msg(gid="room1"):
    """A message shaped exactly like the fanned-in copy a peer agent's own
    daemon delivers: recipient is THIS agent's own identity (not
    "group:<gid>"), thread_id is the group id, sender is another agent."""
    return ChatMessage(
        sender="capauth:opus@skworld.io",
        recipient="capauth:lumina@skworld.io",
        content="Opus: hi everyone",
        thread_id=gid,
    )


@patch("skchat.daemon.SKComms")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_peer_agent_group_message_persisted_even_without_reply(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomms_class,
):
    daemon = ChatDaemon(interval=0.01, quiet=True)

    # Deliver the message only once the mock responder is installed — avoids
    # a race where the genworker could drain (and skip, group_responder still
    # None from real subsystem init not having run/found no configured
    # groups) the message before the test gets a chance to inject the mock.
    message_ready = threading.Event()
    delivered = [False]

    def _poll_side_effect():
        if message_ready.is_set() and not delivered[0]:
            delivered[0] = True
            return [_peer_group_msg()]
        return []

    transport = MagicMock()
    transport.poll_inbox.side_effect = _poll_side_effect

    mock_history = MagicMock()

    mock_skcomms_class.from_config.return_value = mock_skcomms_class
    mock_transport_class.from_config.return_value = transport
    mock_history_class.from_config.return_value = mock_history
    mock_identity.return_value = "capauth:lumina@skworld.io"

    # Loop-breaker vetoed: a peer agent's message never triggers a reply.
    responder = MagicMock()
    responder.respond.return_value = None

    t = threading.Thread(target=daemon.start, daemon=True)
    t.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and daemon._genworker is None:
            time.sleep(0.01)
        assert daemon._genworker is not None

        daemon._test_set_group_responder(responder, agent="lumina")
        message_ready.set()

        deadline = time.time() + 3
        while time.time() < deadline and not mock_history.save.called:
            time.sleep(0.01)

        responder.respond.assert_called()
        # No reply fired (loop-breaker), yet a canonical copy must be saved.
        assert mock_history.save.called, (
            "peer-agent group message was never persisted to shared history "
            "when the loop-breaker vetoed a reply"
        )
        saved = [c.args[0] for c in mock_history.save.call_args_list]
        canonical = [m for m in saved if m.recipient == "group:room1"]
        assert canonical, f"no canonical group:room1 copy saved; saved={saved}"
        assert canonical[0].sender == "capauth:opus@skworld.io"
        assert canonical[0].content == "Opus: hi everyone"
    finally:
        daemon.running = False
        t.join(timeout=3)


@patch("skchat.daemon.SKComms")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_group_message_not_double_persisted_when_already_canonical(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomms_class,
):
    """If a received message already carries the canonical shape
    (recipient == "group:<gid>"), the daemon must not write a second, redundant
    copy."""
    daemon = ChatDaemon(interval=0.01, quiet=True)

    already_canonical = ChatMessage(
        sender="capauth:opus@skworld.io",
        recipient="group:room1",
        content="Opus: hi everyone",
        thread_id="room1",
    )
    message_ready = threading.Event()
    delivered = [False]

    def _poll_side_effect():
        if message_ready.is_set() and not delivered[0]:
            delivered[0] = True
            return [already_canonical]
        return []

    transport = MagicMock()
    transport.poll_inbox.side_effect = _poll_side_effect

    mock_history = MagicMock()

    mock_skcomms_class.from_config.return_value = mock_skcomms_class
    mock_transport_class.from_config.return_value = transport
    mock_history_class.from_config.return_value = mock_history
    mock_identity.return_value = "capauth:lumina@skworld.io"

    responder = MagicMock()
    responder.respond.return_value = None

    t = threading.Thread(target=daemon.start, daemon=True)
    t.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and daemon._genworker is None:
            time.sleep(0.01)
        daemon._test_set_group_responder(responder, agent="lumina")
        message_ready.set()

        deadline = time.time() + 3
        while time.time() < deadline and not responder.respond.called:
            time.sleep(0.01)
        assert responder.respond.called

        # Give the worker a moment to run any (absent) persist step.
        daemon.drain(timeout=5)
        assert not mock_history.save.called, (
            "an already-canonical group message must not be re-saved"
        )
    finally:
        daemon.running = False
        t.join(timeout=3)
