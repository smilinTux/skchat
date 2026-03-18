"""Daemon integration tests — ChatDaemon runs in a background thread.

These tests start a real ChatDaemon loop (with mocked transports) in a
background thread, interact with it over the health endpoint and via the
mock transport inbox, and assert observable side-effects.

Run with:
    cd ~ && python -m pytest skchat/tests/test_daemon_integration.py -v -m integration
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from skchat.daemon import ChatDaemon, DaemonShutdown
from skchat.models import ChatMessage, ContentType, DeliveryStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an unused TCP port on 127.0.0.1."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(condition, timeout: float = 3.0, interval: float = 0.05) -> bool:
    """Busy-poll *condition()* until truthy or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def _build_mock_stack():
    """Return (mock_skcomm, mock_transport, mock_history, mock_advocacy) mocks."""
    mock_skcomm = MagicMock()
    mock_skcomm.router.transports = []  # no WebRTC transport

    mock_transport = MagicMock()
    mock_transport.poll_inbox.return_value = []
    mock_transport.send_and_store = MagicMock()

    mock_history = MagicMock()
    mock_history._store = MagicMock()

    mock_advocacy = MagicMock()
    mock_advocacy.process_message.return_value = None

    return mock_skcomm, mock_transport, mock_history, mock_advocacy


def _start_patches(mock_skcomm, mock_transport, mock_history, mock_advocacy):
    """Activate all daemon dependency patches and return the patcher list.

    Core deps (SKComm, ChatTransport, ChatHistory, identity, AdvocacyEngine) are
    wired to the supplied mock objects.  Optional subsystems that do real I/O
    (watchdog HTTP pings, outbox SQLite, presence file-cache, subprocess) are
    also patched so the daemon loop stays fast and isolated.
    """
    patchers = [
        # Core deps — patch the name as imported into the daemon module namespace,
        # not the skcomm module itself (daemon does `from skcomm import SKComm`).
        patch("skchat.daemon.SKComm"),
        patch("skchat.transport.ChatTransport"),
        patch("skchat.history.ChatHistory"),
        patch("skchat.identity_bridge.get_sovereign_identity"),
        patch("skchat.advocacy.AdvocacyEngine"),
        # Suppress desktop notifications — notify-send can block on headless hosts
        patch("subprocess.run"),
        # Suppress optional subsystems that do real network / SQLite I/O and
        # would slow the poll loop far below the interval=0.05 s setting
        patch("skchat.watchdog.TransportWatchdog"),
        patch("skchat.outbox.OutboxQueue"),
        patch("skchat.presence.PresenceTracker"),
    ]
    mocks = [p.start() for p in patchers]
    p_skcomm_cls, p_transport_cls, p_history_cls, p_id_fn, p_engine_cls, *_ = mocks

    p_skcomm_cls.from_config.return_value = mock_skcomm
    # Daemon calls ChatTransport(skcomm=..., history=..., identity=...) — mock the ctor
    p_transport_cls.return_value = mock_transport
    p_history_cls.from_config.return_value = mock_history
    p_id_fn.return_value = "capauth:bob@skworld.io"
    p_engine_cls.return_value = mock_advocacy

    return patchers


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def inbox_dir(tmp_path):
    """Temporary directory that acts as a file-transport inbox."""
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture()
def test_message() -> ChatMessage:
    """A sample message from Alice to Bob used across integration tests."""
    return ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content="Integration test message from Alice",
        content_type=ContentType.PLAIN,
        delivery_status=DeliveryStatus.SENT,
    )


# ---------------------------------------------------------------------------
# Test 1 — daemon processes a message dropped into the mock inbox dir
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_daemon_processes_inbox_message(inbox_dir, test_message):
    """Daemon picks up a ChatMessage envelope JSON written to *inbox_dir*.

    Steps
    -----
    1. Serialise *test_message* to ``inbox_dir/<id>.json``.
    2. Wire a mock transport whose first ``poll_inbox()`` call reads and
       returns that file; subsequent calls return ``[]``.
    3. Start the daemon in a background thread with all external deps mocked.
    4. Wait ≤ 3 s for ``daemon.total_received`` to reach 1.
    5. Assert the daemon logged / counted the message correctly.
    """
    # 1. Drop the envelope into the mock inbox directory
    envelope_path = inbox_dir / f"{test_message.id}.json"
    envelope_path.write_text(test_message.model_dump_json())

    # 2. Transport: return the envelope on the first poll, then nothing
    first_poll_done = threading.Event()

    def _poll_inbox():
        if not first_poll_done.is_set():
            first_poll_done.set()
            return [ChatMessage.model_validate_json(envelope_path.read_text())]
        return []

    mock_skcomm, mock_transport, mock_history, mock_advocacy = _build_mock_stack()
    mock_transport.poll_inbox.side_effect = _poll_inbox

    # 3. Activate patches (must stay alive across thread boundary)
    patchers = _start_patches(mock_skcomm, mock_transport, mock_history, mock_advocacy)

    # Daemon __init__ calls signal.signal() — must happen on main thread
    daemon = ChatDaemon(interval=0.05, quiet=True)
    exc_holder: list[BaseException] = []

    def _run():
        try:
            # Suppress health server to avoid port-9385 conflicts in CI
            with patch.object(daemon, "_start_health_server"):
                daemon.start()
        except DaemonShutdown:
            pass
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True, name="skchat-daemon-integ")
    thread.start()

    try:
        # 4. Wait up to 3 s for the message to be processed
        processed = _wait_for(lambda: daemon.total_received >= 1, timeout=3.0)
        # Signal the daemon to stop before asserting
        daemon.running = False
        thread.join(timeout=2.0)
    finally:
        for p in patchers:
            p.stop()

    # 5. Assertions
    assert not thread.is_alive(), "Daemon thread did not stop within 2 s"
    assert exc_holder == [], f"Daemon thread raised: {exc_holder[0]}"
    assert processed, (
        f"Daemon did not process the message within 3 s "
        f"(total_received={daemon.total_received}, poll_count={daemon.poll_count})"
    )
    assert daemon.total_received == 1
    assert daemon.poll_count >= 1
    assert first_poll_done.is_set(), "mock transport poll_inbox() was never called"
    # Daemon does not delete inbox files — the transport layer owns the lifecycle
    assert envelope_path.exists()


# ---------------------------------------------------------------------------
# Test 2 — daemon health endpoint returns correct JSON schema
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_daemon_health_endpoint(inbox_dir):
    """GET /health returns valid JSON with the correct schema while running.

    Uses a random free port to avoid conflicts with a real daemon on 9385
    or parallel test runs.
    """
    health_port = _free_port()
    health_started = threading.Event()

    # Wrap _start_health_server to use a free port instead of 9385
    _orig_start_health = ChatDaemon._start_health_server

    def _patched_health(self_d, port=9385):  # noqa: ARG001
        _orig_start_health(self_d, port=health_port)
        health_started.set()

    mock_skcomm, mock_transport, mock_history, mock_advocacy = _build_mock_stack()
    patchers = _start_patches(mock_skcomm, mock_transport, mock_history, mock_advocacy)
    # Add the health-server redirect patch
    health_patcher = patch.object(ChatDaemon, "_start_health_server", _patched_health)
    health_patcher.start()
    patchers.append(health_patcher)

    daemon = ChatDaemon(interval=0.1, quiet=True)
    exc_holder: list[BaseException] = []

    def _run():
        try:
            daemon.start()
        except DaemonShutdown:
            pass
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True, name="skchat-health-integ")
    thread.start()

    body: dict = {}
    status_code: int = 0
    status_404: int = 0

    try:
        # Wait for health server to bind and daemon to execute at least one poll
        assert health_started.wait(timeout=3.0), "Health server did not start within 3 s"
        _wait_for(lambda: daemon.poll_count >= 1, timeout=2.0)

        url = f"http://127.0.0.1:{health_port}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            status_code = resp.status
            body = json.loads(resp.read())

        # 404 for an unknown path (health server only serves /health)
        conn = http.client.HTTPConnection("127.0.0.1", health_port, timeout=2)
        conn.request("GET", "/unknown")
        r404 = conn.getresponse()
        status_404 = r404.status
        conn.close()

    finally:
        daemon.running = False
        thread.join(timeout=2.0)
        for p in patchers:
            p.stop()

    assert not thread.is_alive(), "Daemon thread did not stop within 2 s"
    assert exc_holder == [], f"Daemon thread raised: {exc_holder[0]}"

    # /health schema
    assert status_code == 200
    assert body["status"] in ("ok", "stopping")
    assert isinstance(body["uptime_s"], int) and body["uptime_s"] >= 0
    assert isinstance(body["messages_received"], int)
    assert isinstance(body["transport_ok"], bool)
    assert "last_poll_at" in body

    # Non-existent path → 404
    assert status_404 == 404


# ---------------------------------------------------------------------------
# Test 3 — daemon counts zero messages when inbox is empty
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_daemon_empty_inbox_no_messages(inbox_dir):
    """Daemon polling an empty inbox must keep total_received at 0.

    Runs for several poll cycles to confirm the daemon stays stable with
    no messages available.
    """
    mock_skcomm, mock_transport, mock_history, mock_advocacy = _build_mock_stack()
    # poll_inbox already returns [] from _build_mock_stack defaults

    patchers = _start_patches(mock_skcomm, mock_transport, mock_history, mock_advocacy)
    daemon = ChatDaemon(interval=0.05, quiet=True)
    exc_holder: list[BaseException] = []

    def _run():
        try:
            with patch.object(daemon, "_start_health_server"):
                daemon.start()
        except DaemonShutdown:
            pass
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True, name="skchat-empty-integ")
    thread.start()

    try:
        # Let it run for a few cycles
        _wait_for(lambda: daemon.poll_count >= 5, timeout=3.0)
        daemon.running = False
        thread.join(timeout=2.0)
    finally:
        for p in patchers:
            p.stop()

    assert not thread.is_alive(), "Daemon thread did not stop within 2 s"
    assert exc_holder == [], f"Daemon thread raised: {exc_holder[0]}"
    assert daemon.total_received == 0
    assert daemon.poll_count >= 5
