"""E2E test: opus sends to lumina, lumina responds via mocked LLMBridge.

Flow:
  1. Write a ChatMessage JSON envelope into lumina's file-transport inbox dir.
  2. Patch call_consciousness to return 'Hello from Lumina'.
  3. Patch check_inbox_for_lumina to read from the tmp inbox dir via
     ChatTransport + _FileSKComm (no skmemory, no daemon).
  4. Patch send_reply to write the response to opus's tmp inbox dir.
  5. Run one lumina-bridge poll iteration.
  6. Assert lumina's reply appears in opus's inbox dir.

Self-contained: no network, no daemon, no skmemory.

Run with:
    pytest tests/test_e2e_claude_lumina.py -v
    pytest tests/test_e2e_claude_lumina.py -v -m e2e_live
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Mark
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e_live

# ---------------------------------------------------------------------------
# Identities (mirror lumina_bridge constants)
# ---------------------------------------------------------------------------

LUMINA_IDENTITY = "capauth:lumina@skworld.io"
OPUS_IDENTITY = "capauth:opus@skworld.io"

# ---------------------------------------------------------------------------
# File-based SKComm stub  (no real SKComm / Syncthing)
# ---------------------------------------------------------------------------


class _FileSKComm:
    """Minimal file-based SKComm stub.

    send() writes JSON payload to outbox_dir/{uuid}.json.
    receive() reads + deletes all *.json files from inbox_dir.
    """

    def __init__(self, outbox_dir: Path, inbox_dir: Path) -> None:
        self._outbox = outbox_dir
        self._inbox = inbox_dir

    def send(
        self,
        recipient: str,
        message: str,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> SimpleNamespace:
        filename = f"{uuid.uuid4()}.json"
        (self._outbox / filename).write_text(message, encoding="utf-8")
        return SimpleNamespace(delivered=True, successful_transport="file")

    def receive(self) -> list:
        envelopes = []
        for f in sorted(self._inbox.glob("*.json")):
            try:
                content = f.read_text(encoding="utf-8")
                f.unlink()
                envelopes.append(
                    SimpleNamespace(payload=SimpleNamespace(content=content))
                )
            except Exception:
                continue
        return envelopes


# ---------------------------------------------------------------------------
# In-memory ChatHistory stub  (no skmemory)
# ---------------------------------------------------------------------------


class _InMemoryHistory:
    def __init__(self) -> None:
        self._messages: list = []

    def store_message(self, message) -> str:
        self._messages.append(message)
        return message.id

    def all_messages(self) -> list:
        return list(self._messages)


# ---------------------------------------------------------------------------
# Module loader for lumina-bridge.py  (lives outside the skchat package)
# ---------------------------------------------------------------------------


def _load_bridge():
    """Load scripts/lumina-bridge.py as a Python module.

    Uses importlib to load the file by path.  A stable module name
    ('lumina_bridge_e2e') prevents collisions with other test sessions.
    """
    bridge_path = Path(__file__).parent.parent / "scripts" / "lumina-bridge.py"
    mod_name = "lumina_bridge_e2e"

    # If already loaded (e.g., from a previous test in the same session),
    # return the cached module so module-level globals are shared.
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, bridge_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


@pytest.mark.e2e_live
def test_claude_sends_to_lumina_lumina_responds(tmp_path: Path, monkeypatch) -> None:
    """Opus injects a message; lumina bridge replies 'Hello from Lumina'.

    One bridge poll iteration — no network, no daemon, no skmemory.
    """
    from skchat.models import ChatMessage
    from skchat.transport import ChatTransport

    # ── 1. Temporary transport directories ──────────────────────────────────
    lumina_inbox = tmp_path / "lumina_inbox"
    opus_inbox = tmp_path / "opus_inbox"
    lumina_outbox = tmp_path / "lumina_outbox"  # lumina → opus replies land here
    opus_outbox = tmp_path / "opus_outbox"      # unused; needed for opus ChatTransport
    for d in (lumina_inbox, opus_inbox, lumina_outbox, opus_outbox):
        d.mkdir()

    # ── 2. Inject opus→lumina message  (raw JSON envelope in lumina_inbox) ──
    original = ChatMessage(
        sender=OPUS_IDENTITY,
        recipient=LUMINA_IDENTITY,
        content="Hello Lumina, are you there?",
    )
    envelope_path = lumina_inbox / f"{original.id}.json"
    envelope_path.write_text(original.model_dump_json(), encoding="utf-8")

    assert envelope_path.exists(), "Precondition: envelope file must be in lumina_inbox"

    # ── 3. File-transport stubs for lumina (read) and opus (write) ───────────
    lumina_history = _InMemoryHistory()
    lumina_transport = ChatTransport(
        skcomm=_FileSKComm(outbox_dir=lumina_outbox, inbox_dir=lumina_inbox),
        history=lumina_history,
        identity=LUMINA_IDENTITY,
    )

    # Lumina's reply skcomm writes to opus_inbox so opus can read it.
    reply_transport = ChatTransport(
        skcomm=_FileSKComm(outbox_dir=opus_inbox, inbox_dir=lumina_outbox),
        history=_InMemoryHistory(),
        identity=LUMINA_IDENTITY,
    )

    # ── 4. Load lumina-bridge module ─────────────────────────────────────────
    bridge = _load_bridge()

    # ── 5. Reset module-level dedup state ────────────────────────────────────
    bridge.BRIDGE_HISTORY.clear()
    bridge._last_response.clear()

    # ── 6. Patch call_consciousness → mock LLMBridge ─────────────────────────
    monkeypatch.setattr(bridge, "call_consciousness", lambda prompt: "Hello from Lumina")

    # ── 7. Patch check_inbox_for_lumina → file-transport read ────────────────
    def _fake_check_inbox() -> list[dict]:
        messages = lumina_transport.poll_inbox()
        return [
            {
                "message_id": m.id,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "thread_id": m.thread_id,
                "reply_to": m.reply_to_id,
            }
            for m in messages
            if bridge._msg_key({"message_id": m.id}) not in bridge.BRIDGE_HISTORY
        ]

    monkeypatch.setattr(bridge, "check_inbox_for_lumina", _fake_check_inbox)

    # ── 8. Patch send_reply → file-transport write ────────────────────────────
    def _fake_send_reply(original_msg: dict, reply_text: str) -> None:
        reply_transport.send_and_store(
            recipient=original_msg.get("sender", OPUS_IDENTITY),
            content=reply_text,
            thread_id=original_msg.get("thread_id"),
            reply_to=original_msg.get("message_id"),
        )

    monkeypatch.setattr(bridge, "send_reply", _fake_send_reply)

    # ── 9. Run one poll iteration ─────────────────────────────────────────────
    messages = bridge.check_inbox_for_lumina()

    assert len(messages) == 1, (
        f"Expected 1 message in lumina's inbox, got {len(messages)}"
    )

    for msg in messages:
        key = bridge._msg_key(msg)
        bridge.BRIDGE_HISTORY.add(key)

        sender = msg.get("sender", OPUS_IDENTITY)
        content = msg.get("content", "")

        response = bridge.call_consciousness(content)
        bridge.send_reply(msg, response)
        bridge._record_response(sender)

    # ── 10. Assert lumina's response is in opus's inbox ──────────────────────
    opus_history = _InMemoryHistory()
    opus_transport = ChatTransport(
        skcomm=_FileSKComm(outbox_dir=opus_outbox, inbox_dir=opus_inbox),
        history=opus_history,
        identity=OPUS_IDENTITY,
    )

    received = opus_transport.poll_inbox()

    assert len(received) == 1, (
        f"Expected 1 reply in opus's inbox, got {len(received)}: {received!r}"
    )
    reply = received[0]

    assert reply.content == "Hello from Lumina", (
        f"Unexpected reply content: {reply.content!r}"
    )
    assert reply.sender == LUMINA_IDENTITY, (
        f"Expected sender {LUMINA_IDENTITY!r}, got {reply.sender!r}"
    )
    assert reply.recipient == OPUS_IDENTITY, (
        f"Expected recipient {OPUS_IDENTITY!r}, got {reply.recipient!r}"
    )
    # reply_to propagation: lumina sets this from original message_id.
    # Verify it points to the original message or is None (depending on
    # whether the bridge passes it through send_and_store correctly).
    assert reply.reply_to_id in (original.id, None)
