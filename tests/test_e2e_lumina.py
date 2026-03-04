"""E2E live test: opus sends to lumina, lumina responds.

Requires two live services:
- skchat daemon on :9385  (``skchat daemon start`` run from ~/)
- lumina-bridge on :9386  (``skchat-lumina-bridge.service`` or
  ``python scripts/lumina-bridge.py``)

Both checks skip automatically when either service is absent so the suite
stays green in CI environments without the daemons.

Delivery path used in this test
--------------------------------
Rather than calling ``skchat send`` (which blocks on SKComm transport
initialisation), we write a properly-formatted SKComm envelope directly to
``~/.skcomm/outbox/<uuid>.skc.json``.  The lumina-bridge's
``poll_outbox_for_lumina()`` scans that directory on every poll cycle and
hands the message to the consciousness loop without going through the SKComm
daemon.  Lumina's reply is written to ``~/.skchat/history/<today>.jsonl`` via
``deliver_reply_to_inbox() → history.save()``, which is where we poll for it.

Run with:
    cd ~ && python -m pytest /home/cbrd21/dkloud.douno.it/p/smilintux-org/skchat/tests/test_e2e_lumina.py -v
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# pytest mark — whole module is e2e_live
# ---------------------------------------------------------------------------

pytestmark = [pytest.mark.e2e_live, pytest.mark.integration]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LUMINA_IDENTITY = "capauth:lumina@skworld.io"
OPUS_IDENTITY = "capauth:opus@skworld.io"

TIMEOUT_S = 15
POLL_INTERVAL_S = 1.5

SKCHAT_HOME = Path.home() / ".skchat"
SKCOMM_OUTBOX = Path.home() / ".skcomm" / "outbox"

DAEMON_HEALTH_URL = "http://127.0.0.1:9385/health"
LUMINA_HEALTH_URL = "http://127.0.0.1:9386/health"

# ---------------------------------------------------------------------------
# Service-availability helpers
# ---------------------------------------------------------------------------


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    """Return True if *url* responds with HTTP 2xx within *timeout* seconds."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Envelope writer
# ---------------------------------------------------------------------------


def _write_outbox_envelope(
    sender: str,
    recipient: str,
    content: str,
    thread_id: str | None = None,
) -> tuple[str, Path]:
    """Write a SKComm envelope to ~/.skcomm/outbox/ for lumina-bridge to pick up.

    The lumina-bridge's ``poll_outbox_for_lumina()`` scans ``*.skc.json``
    files; it tries to parse ``payload.content`` as a ChatMessage JSON first
    and falls back to treating it as plain text.  We embed a full ChatMessage
    so ``message_id`` propagates correctly.

    Returns:
        (message_id, envelope_path) — the ChatMessage UUID and the file written.
    """
    from skchat.models import ChatMessage, ContentType

    SKCOMM_OUTBOX.mkdir(parents=True, exist_ok=True)

    msg = ChatMessage(
        sender=sender,
        recipient=recipient,
        content=content,
        content_type=ContentType.MARKDOWN,
        thread_id=thread_id,
    )
    envelope_id = str(uuid.uuid4())

    envelope = {
        "skcomm_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": sender,
        "recipient": recipient,
        "payload": {
            "content": msg.model_dump_json(),
            "content_type": "text",
            "encrypted": False,
            "compressed": False,
            "signature": None,
        },
        "routing": {
            "mode": "failover",
            "preferred_transports": [],
            "retry_max": 0,
            "retry_backoff": [],
            "ttl": 300,
            "ack_requested": False,
        },
        "metadata": {
            "thread_id": thread_id,
            "in_reply_to": None,
            "urgency": "normal",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": None,
            "attempt": 0,
            "delivered_via": "e2e_test",
        },
    }

    filename = f"{envelope_id}.skc.json"
    path = SKCOMM_OUTBOX / filename
    tmp = SKCOMM_OUTBOX / f".{filename}.tmp"
    tmp.write_bytes(json.dumps(envelope, indent=2).encode("utf-8"))
    tmp.rename(path)

    return str(msg.id), path


# ---------------------------------------------------------------------------
# History reader — scans JSONL directly, no skmemory import required
# ---------------------------------------------------------------------------


def _messages_from_lumina_since(since: datetime) -> list[dict]:
    """Scan today's JSONL history for messages sent by Lumina after *since*.

    Args:
        since: UTC datetime; only messages with ``timestamp >= since`` returned.

    Returns:
        List of raw message dicts, in file order.
    """
    history_dir = SKCHAT_HOME / "history"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    jsonl_path = history_dir / f"{today}.jsonl"

    if not jsonl_path.exists():
        return []

    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError:
        return []

    results: list[dict] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if msg.get("sender") != LUMINA_IDENTITY:
            continue
        ts_str = msg.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= since:
            results.append(msg)

    return results


# ---------------------------------------------------------------------------
# E2E live test
# ---------------------------------------------------------------------------


@pytest.mark.e2e_live
def test_opus_sends_to_lumina_lumina_responds() -> None:
    """Opus sends a unique message to Lumina; asserts a reply within 15 s.

    Flow
    ----
    1. Skip unless both ``skchat daemon`` (:9385) and ``lumina-bridge``
       (:9386) are reachable.
    2. Compose a unique ``@lumina ping e2e-<nonce>`` message — the nonce
       prevents false matches against stale history entries.
    3. Write a ``*.skc.json`` envelope directly to ``~/.skcomm/outbox/``
       (the path that ``poll_outbox_for_lumina()`` scans on every cycle).
       This bypasses the SKComm transport daemon, which can block indefinitely
       when its backing network is unavailable.
    4. Poll ``~/.skchat/history/<today>.jsonl`` every 1.5 s for up to 15 s,
       looking for a message whose sender is Lumina and whose timestamp
       post-dates the write.
    5. Assert the reply is non-empty and attributed to Lumina.
    """
    # ── 1. Service availability guards ─────────────────────────────────────
    if not _http_ok(DAEMON_HEALTH_URL):
        pytest.skip(
            "skchat daemon not reachable on :9385 — "
            "run 'skchat daemon start' from ~/ first"
        )
    if not _http_ok(LUMINA_HEALTH_URL):
        pytest.skip(
            "lumina-bridge not reachable on :9386 — "
            "run 'python scripts/lumina-bridge.py' or start the systemd unit first"
        )

    # ── 2. Unique nonce prevents matching stale history entries ────────────
    nonce = uuid.uuid4().hex[:12]
    content = f"@lumina ping e2e-{nonce}"

    # ── 3. Deliver via outbox file — no SKComm daemon required ─────────────
    send_time = datetime.now(timezone.utc)
    message_id, envelope_path = _write_outbox_envelope(
        sender=OPUS_IDENTITY,
        recipient=LUMINA_IDENTITY,
        content=content,
    )

    assert envelope_path.exists(), (
        f"Envelope not written to outbox: {envelope_path}"
    )

    # ── 4. Poll for Lumina's reply ──────────────────────────────────────────
    deadline = time.monotonic() + TIMEOUT_S
    reply: dict | None = None

    while time.monotonic() < deadline:
        candidates = _messages_from_lumina_since(since=send_time)
        if candidates:
            reply = candidates[0]
            break
        time.sleep(POLL_INTERVAL_S)

    # ── 5. Assertions ───────────────────────────────────────────────────────
    assert reply is not None, (
        f"No reply from {LUMINA_IDENTITY!r} within {TIMEOUT_S}s.\n"
        f"Sent: {content!r} (id={message_id}) at {send_time.isoformat()}\n"
        f"Outbox file exists: {envelope_path.exists()}\n"
        f"Check ~/.skchat/lumina-bridge.log and ~/.skchat/history/ for details."
    )
    assert reply.get("sender") == LUMINA_IDENTITY, (
        f"Expected sender {LUMINA_IDENTITY!r}, got {reply.get('sender')!r}"
    )
    assert reply.get("content", "").strip(), (
        f"Lumina replied but content is empty: {reply!r}"
    )
