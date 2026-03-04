"""E2E live tests: claude sends to lumina, lumina responds.

test_send_to_lumina_and_receive_reply
    1. Builds a ChatMessage (claude → lumina).
    2. Persists it via ChatHistory.save() (JSONL).
    3. Also writes a raw SKComm envelope to ~/.skcomm/outbox/ so that
       lumina's poll_outbox_for_lumina() picks it up without Syncthing.
    4. Polls ChatHistory.load() (JSONL) for lumina's reply within 120s.
       Lumina's bridge calls history.save() when replying, so JSONL
       polling captures the response.

test_advocacy_engine_responds
    1. Builds a ChatMessage containing '@opus' (trigger prefix).
    2. Calls AdvocacyEngine.process_message() in a thread capped at 30s
       (handles slow LLM calls through skcapstone).
    3. Asserts a non-empty string reply is returned.

Both tests are skipped when the skchat daemon PID file is absent.

Run with:
    pytest tests/test_e2e_chat.py -v -m e2e_live
    cd ~ && python -m pytest <path>/tests/test_e2e_chat.py -v -m e2e_live
"""

from __future__ import annotations

import concurrent.futures
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from skchat.models import ChatMessage

# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

CLAUDE_IDENTITY = "capauth:claude@skworld.io"
LUMINA_IDENTITY = "capauth:lumina@skworld.io"

# File paths mirrored from lumina-bridge.py
_SKCOMM_OUTBOX = Path("~/.skcomm/outbox").expanduser()
_SKCHAT_HISTORY_DIR = Path("~/.skchat/history").expanduser()

# ---------------------------------------------------------------------------
# pytest mark
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e_live

# ---------------------------------------------------------------------------
# Daemon-running check (evaluated once at collection time)
# ---------------------------------------------------------------------------


def _daemon_running() -> bool:
    """Return True when the skchat daemon process is alive."""
    try:
        from skchat.daemon import is_running  # type: ignore[import]

        return is_running()
    except Exception:
        return False


_DAEMON_UP: bool = _daemon_running()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_to_lumina_outbox(message: ChatMessage) -> None:
    """Write *message* as a SKComm envelope to the local outbox.

    lumina-bridge.py's poll_outbox_for_lumina() scans ~/.skcomm/outbox/
    for ``*.skc.json`` files whose recipient is lumina.  Writing the
    envelope here triggers lumina without requiring Syncthing.

    Falls back silently if the outbox directory cannot be created.

    Args:
        message: Outbound ChatMessage to envelope-wrap.
    """
    try:
        _SKCOMM_OUTBOX.mkdir(parents=True, exist_ok=True)
        envelope = {
            "skcomm_version": "1.0.0",
            "envelope_id": message.id,
            "sender": message.sender,
            "recipient": message.recipient,
            "payload": {"content": message.model_dump_json()},
            "timestamp": message.timestamp.isoformat(),
        }
        outfile = _SKCOMM_OUTBOX / f"{message.id}.skc.json"
        outfile.write_text(json.dumps(envelope), encoding="utf-8")
    except Exception:
        pass  # best-effort; test will still wait and may time out


def _poll_jsonl_for_reply(
    history_dir: Path,
    sender: str,
    recipient: str,
    since: datetime,
    timeout_s: float,
    interval_s: float = 3.0,
) -> Optional[ChatMessage]:
    """Block until a reply from *sender* to *recipient* appears in JSONL.

    lumina-bridge.py calls ChatHistory.save() on every reply, writing to
    the same JSONL history directory.  ChatHistory.load() re-reads those
    files on each call so new replies are visible without a cache flush.

    Args:
        history_dir: Path to the ~/.skchat/history/ JSONL directory.
        sender: Expected reply sender identity URI.
        recipient: Expected reply recipient identity URI.
        since: Only consider messages with timestamp >= this value.
        timeout_s: Maximum wait in seconds before returning None.
        interval_s: Sleep between polls.

    Returns:
        The first matching ChatMessage, or None on timeout.
    """
    from skchat.history import ChatHistory

    history = ChatHistory(history_dir=history_dir)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        candidates = history.load(since=since, peer=sender, limit=50)
        for msg in candidates:
            if msg.sender == sender and msg.recipient == recipient:
                return msg
        time.sleep(interval_s)
    return None


# ---------------------------------------------------------------------------
# Test: claude → lumina → reply
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DAEMON_UP, reason="skchat daemon not running")
@pytest.mark.e2e_live
def test_send_to_lumina_and_receive_reply() -> None:
    """claude saves a message; lumina daemon replies within 120 s.

    Flow
    ────
    1. Build a unique ChatMessage (claude → lumina).
    2. Persist outbound record via ChatHistory.save() (JSONL).
    3. Inject an SKComm envelope into ~/.skcomm/outbox/ so lumina's
       poll_outbox_for_lumina() loop picks it up without Syncthing.
    4. Poll ChatHistory.load() every 3 s for up to 120 s.
    5. Assert a reply appears with sender=lumina, recipient=claude.
    """
    from skchat.history import ChatHistory

    history = ChatHistory(history_dir=_SKCHAT_HISTORY_DIR)
    before = datetime.now(timezone.utc)

    nonce = uuid.uuid4().hex[:8]
    msg = ChatMessage(
        sender=CLAUDE_IDENTITY,
        recipient=LUMINA_IDENTITY,
        content=f"E2E ping {nonce} — please reply.",
    )

    # 1 — persist outbound record (JSONL)
    history.save(msg)

    # 2 — inject into file transport so lumina picks it up
    _inject_to_lumina_outbox(msg)

    # 3 — wait for reply
    reply = _poll_jsonl_for_reply(
        history_dir=_SKCHAT_HISTORY_DIR,
        sender=LUMINA_IDENTITY,
        recipient=CLAUDE_IDENTITY,
        since=before,
        timeout_s=120.0,
        interval_s=3.0,
    )

    assert reply is not None, (
        f"No reply from {LUMINA_IDENTITY} within 120 s "
        f"(nonce={nonce}, sent_at={before.isoformat()})"
    )
    assert reply.sender == LUMINA_IDENTITY, (
        f"Expected sender={LUMINA_IDENTITY!r}, got {reply.sender!r}"
    )
    assert reply.recipient == CLAUDE_IDENTITY, (
        f"Expected recipient={CLAUDE_IDENTITY!r}, got {reply.recipient!r}"
    )
    assert reply.content.strip(), "Lumina reply content must not be empty"


# ---------------------------------------------------------------------------
# Test: @opus mention → AdvocacyEngine auto-reply
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DAEMON_UP, reason="skchat daemon not running")
@pytest.mark.e2e_live
def test_advocacy_engine_responds() -> None:
    """AdvocacyEngine returns a non-empty reply to an @opus mention within 30 s.

    The consciousness call through skcapstone can be slow; the reply is
    awaited in a worker thread so we get a precise wall-clock timeout.

    Flow
    ────
    1. Build a ChatMessage containing '@opus' (an AdvocacyEngine trigger).
    2. Run AdvocacyEngine.process_message() in a thread with a 30 s cap.
    3. Assert the return value is a non-empty string.
    """
    from skchat.advocacy import AdvocacyEngine

    msg = ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient=CLAUDE_IDENTITY,
        content="@opus what is the sovereign AI philosophy?",
    )

    engine = AdvocacyEngine(identity=CLAUDE_IDENTITY)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.process_message, msg)
        try:
            reply = future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            pytest.fail(
                "AdvocacyEngine.process_message() did not return within 30 s"
            )

    assert reply is not None, (
        "AdvocacyEngine.process_message() returned None for a message with @opus"
    )
    assert isinstance(reply, str) and reply.strip(), (
        f"Expected a non-empty string reply, got: {reply!r}"
    )
