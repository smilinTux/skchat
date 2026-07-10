"""Tests for the daemon's async-generation plumbing (Task 2 of the async-gen plan).

Proves the poll loop enqueues received messages onto a single-worker FIFO
instead of running the blocking generate->send->store chain inline, so
back-to-back messages are all received (poll advances) even while one reply
is still generating.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from skchat.daemon import ChatDaemon
from skchat.models import ChatMessage


def _dm(content="hello", sender="capauth:chef@skworld.io"):
    return ChatMessage(sender=sender, recipient="capauth:lumina@skworld.io", content=content)


def test_daemon_has_async_generation_plumbing():
    """__init__ wires the FIFO queue + send lock; worker starts unset."""
    daemon = ChatDaemon(interval=10, quiet=True)
    assert daemon._genqueue.empty()
    assert daemon._genworker is None
    assert daemon._send_lock is not None


def test_drain_returns_immediately_when_queue_empty():
    daemon = ChatDaemon(interval=10, quiet=True)
    # Should not hang — nothing was ever enqueued.
    daemon.drain(timeout=1)


@patch("skchat.daemon.SKComms")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_poll_loop_does_not_block_on_generation(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomms_class,
):
    """While one reply is generating (held), the poll loop keeps polling and the
    worker eventually answers - proving generation is off the poll thread."""
    daemon = ChatDaemon(interval=0.01, quiet=True)

    gate = threading.Event()  # holds the DM responder inside "generation"
    sent = []

    # Deliver the DM only once the test has installed the mock responder (via
    # `message_ready`) — avoids a race where the genworker could drain the
    # message with group_responder still None (real subsystem init not yet
    # finished) before the test gets a chance to inject the mock.
    message_ready = threading.Event()
    delivered = [False]

    def _poll_side_effect():
        if message_ready.is_set() and not delivered[0]:
            delivered[0] = True
            return [_dm("first")]
        return []

    transport = MagicMock()
    transport.poll_inbox.side_effect = _poll_side_effect
    transport.send_message.side_effect = lambda m: sent.append(m)

    mock_skcomms_class.from_config.return_value = mock_skcomms_class
    mock_transport_class.from_config.return_value = transport
    mock_history_class.from_config.return_value = MagicMock()
    mock_identity.return_value = "capauth:lumina@skworld.io"

    # group_responder: not a group message; respond_direct blocks on the gate.
    responder = MagicMock()

    def slow_direct(msg):
        gate.wait(timeout=5)
        return "answered"

    responder.respond_direct.side_effect = slow_direct

    t = threading.Thread(target=daemon.start, daemon=True)
    t.start()
    try:
        # Wait until the daemon installed the async worker + the test seam.
        deadline = time.time() + 3
        while time.time() < deadline and daemon._genworker is None:
            time.sleep(0.01)
        assert daemon._genworker is not None, "worker thread never started"

        # Force the mock responder into the running daemon, THEN allow the
        # transport to hand over the DM (see message_ready above).
        daemon._test_set_group_responder(responder, agent="lumina")
        polls_before = daemon.poll_count
        message_ready.set()

        # Poll loop must advance well past delivery while generation is gated
        # (proves receive/poll is not blocked by the ~10s-shaped generate call).
        deadline = time.time() + 3
        while time.time() < deadline and daemon.poll_count < polls_before + 3:
            time.sleep(0.01)
        assert daemon.poll_count >= polls_before + 3, "poll loop blocked on generation"
        assert sent == [], "reply sent before gate released"

        gate.set()  # release generation
        daemon.drain(timeout=5)  # wait for the worker to finish the job
        assert any(m.content == "answered" for m in sent)
    finally:
        daemon.running = False
        t.join(timeout=3)


@patch("skchat.daemon.SKComms")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_receive_log_line_emitted_before_worker_drains(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomms_class,
    capsys,
):
    """The per-message '[sender] preview' receive log (used for ops
    verification via daemon.log) must appear from the poll loop itself, even
    while the worker is gated — proving it was restored at enqueue time, not
    lost inside the now-deferred _process()."""
    daemon = ChatDaemon(interval=0.01, quiet=False)

    gate = threading.Event()
    polls = [[_dm("hello there", sender="capauth:chef@skworld.io")]]
    transport = MagicMock()
    transport.poll_inbox.side_effect = lambda: polls.pop(0) if polls else []

    mock_skcomms_class.from_config.return_value = mock_skcomms_class
    mock_transport_class.from_config.return_value = transport
    mock_history_class.from_config.return_value = MagicMock()
    mock_identity.return_value = "capauth:lumina@skworld.io"

    responder = MagicMock()
    responder.respond_direct.side_effect = lambda msg: gate.wait(timeout=5) and "answered"

    t = threading.Thread(target=daemon.start, daemon=True)
    t.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and daemon._genworker is None:
            time.sleep(0.01)
        daemon._test_set_group_responder(responder, agent="lumina")

        # Wait until the receive-time log line actually appears. total_received
        # increments a statement earlier than the per-message log/print, so a
        # single immediate capsys read can race the print — accumulate output
        # across a short poll instead of reading once.
        captured = ""
        deadline = time.time() + 3
        while time.time() < deadline:
            captured += capsys.readouterr().out
            if "[chef] hello there" in captured:
                break
            time.sleep(0.01)
        assert "[chef] hello there" in captured, (
            "receive-time log line missing from daemon output — Task 1's "
            "extraction dropped it and it must be restored in the poll loop"
        )
        gate.set()
        daemon.drain(timeout=5)
    finally:
        daemon.running = False
        t.join(timeout=3)


@patch("skchat.daemon.SKComms")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_stop_drains_and_joins_worker(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomms_class,
):
    """stop() drains the queue and joins the worker thread cleanly."""
    daemon = ChatDaemon(interval=0.01, quiet=True)

    transport = MagicMock()
    transport.poll_inbox.side_effect = lambda: []

    mock_skcomms_class.from_config.return_value = mock_skcomms_class
    mock_transport_class.from_config.return_value = transport
    mock_history_class.from_config.return_value = MagicMock()
    mock_identity.return_value = "capauth:lumina@skworld.io"

    t = threading.Thread(target=daemon.start, daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and daemon._genworker is None:
        time.sleep(0.01)
    assert daemon._genworker is not None

    daemon.stop()
    assert daemon.running is False
    assert not daemon._genworker.is_alive()
    t.join(timeout=3)
