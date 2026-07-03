"""Tests for the lumina-bridge local loopback delivery path.

Single-machine testing (no Syncthing) relies on the bridge:

  1. reading envelopes addressed to Lumina straight out of the local
     ``~/.skcomms/outbox/`` (:func:`poll_outbox_for_lumina`) and the
     file-transport inbox (:func:`poll_inbox_file_for_lumina`), and
  2. short-circuiting the reply back into ``~/.skcomms/inbox/`` so the Opus
     daemon receives it (:func:`deliver_reply_to_inbox`).

These tests exercise that loopback against *temp* directories — never the
live ``~/.skcomms`` — by monkeypatching the module-level path constants.
The bridge script is hyphenated, so it is loaded via importlib (matching
tests/test_lumina_bridge_dryrun.py).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import time
import uuid

import pytest

_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "lumina-bridge.py"
)

LUMINA = "capauth:lumina@skworld.io"
OPUS = "capauth:opus@skworld.io"


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """Load scripts/lumina-bridge.py with all skcomms paths pointed at tmp.

    Guarantees the loopback tests NEVER read or write the live ~/.skcomms
    directories, and start from an empty processed-history set.
    """
    spec = importlib.util.spec_from_file_location(
        "lumina_bridge_loopback_under_test", _BRIDGE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    outbox = tmp_path / "skcomms" / "outbox"
    inbox = tmp_path / "skcomms" / "inbox"
    file_inbox = tmp_path / "skcomms" / "transport" / "file" / "inbox"
    for d in (outbox, inbox, file_inbox):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mod, "OUTBOX_PATH", outbox)
    monkeypatch.setattr(mod, "INBOX_PATH", inbox)
    monkeypatch.setattr(mod, "_FILE_TRANSPORT_INBOX", file_inbox)
    # Fresh dedup state so a real ~/.skchat/lumina-processed.json can't hide msgs.
    monkeypatch.setattr(mod, "BRIDGE_HISTORY", set())

    yield mod
    mod.DRY_RUN = False


def _write_envelope(
    directory: pathlib.Path,
    *,
    recipient: str,
    sender: str = OPUS,
    content: str = "hello lumina",
    content_type: str = "text",
    envelope_id: str | None = None,
) -> pathlib.Path:
    """Write a minimal skcomms .skc.json envelope and return its path."""
    envelope_id = envelope_id or str(uuid.uuid4())
    envelope = {
        "skcomms_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": sender,
        "recipient": recipient,
        "payload": {"content": content, "content_type": content_type},
    }
    path = directory / f"{envelope_id}.skc.json"
    path.write_text(json.dumps(envelope))
    return path


# ─── poll_outbox_for_lumina ──────────────────────────────────────────────────


class TestPollOutbox:
    def test_finds_envelope_addressed_to_lumina(self, bridge):
        _write_envelope(bridge.OUTBOX_PATH, recipient=LUMINA, content="ping")
        found = bridge.poll_outbox_for_lumina()
        assert len(found) == 1
        assert found[0]["recipient"] == LUMINA
        assert found[0]["sender"] == OPUS
        assert found[0]["content"] == "ping"
        # carries the on-disk path so _consume_outbox_file can remove it
        assert found[0]["_outbox_file"].endswith(".skc.json")

    def test_ignores_envelopes_for_other_recipients(self, bridge):
        _write_envelope(bridge.OUTBOX_PATH, recipient=OPUS, content="not for lumina")
        assert bridge.poll_outbox_for_lumina() == []

    def test_accepts_bare_and_domain_identity_variants(self, bridge):
        _write_envelope(bridge.OUTBOX_PATH, recipient="lumina", content="bare")
        _write_envelope(
            bridge.OUTBOX_PATH, recipient="lumina@skworld.io", content="domain"
        )
        contents = {m["content"] for m in bridge.poll_outbox_for_lumina()}
        assert contents == {"bare", "domain"}

    def test_skips_control_envelope_types(self, bridge):
        for ct in ("ack", "heartbeat", "read_receipt"):
            _write_envelope(
                bridge.OUTBOX_PATH, recipient=LUMINA, content="x", content_type=ct
            )
        assert bridge.poll_outbox_for_lumina() == []

    def test_extracts_content_from_serialized_chatmessage(self, bridge):
        from skchat.models import ChatMessage

        inner = ChatMessage(sender=OPUS, recipient=LUMINA, content="wrapped body")
        _write_envelope(
            bridge.OUTBOX_PATH, recipient=LUMINA, content=inner.model_dump_json()
        )
        found = bridge.poll_outbox_for_lumina()
        assert len(found) == 1
        assert found[0]["content"] == "wrapped body"
        assert found[0]["message_id"] == str(inner.id)

    def test_already_processed_envelope_is_skipped_and_cleaned(self, bridge):
        path = _write_envelope(bridge.OUTBOX_PATH, recipient=LUMINA)
        eid = json.loads(path.read_text())["envelope_id"]
        bridge.BRIDGE_HISTORY.add(f"outbox:{eid}")
        assert bridge.poll_outbox_for_lumina() == []
        # stale file is removed
        assert not path.exists()

    def test_missing_outbox_dir_returns_empty(self, bridge):
        import shutil

        shutil.rmtree(bridge.OUTBOX_PATH)
        assert bridge.poll_outbox_for_lumina() == []

    def test_malformed_json_is_ignored(self, bridge):
        (bridge.OUTBOX_PATH / "garbage.skc.json").write_text("{not json")
        assert bridge.poll_outbox_for_lumina() == []


# ─── poll_inbox_file_for_lumina ──────────────────────────────────────────────


class TestPollFileInbox:
    def test_finds_loopback_envelope_in_transport_inbox(self, bridge):
        _write_envelope(
            bridge._FILE_TRANSPORT_INBOX, recipient=LUMINA, content="loopback"
        )
        found = bridge.poll_inbox_file_for_lumina()
        assert len(found) == 1
        assert found[0]["content"] == "loopback"
        assert found[0]["_inbox_file"].endswith(".skc.json")

    def test_finds_envelope_in_shared_inbox(self, bridge):
        _write_envelope(bridge.INBOX_PATH, recipient=LUMINA, content="via inbox")
        contents = {m["content"] for m in bridge.poll_inbox_file_for_lumina()}
        assert "via inbox" in contents

    def test_ignores_other_recipients(self, bridge):
        _write_envelope(bridge._FILE_TRANSPORT_INBOX, recipient=OPUS)
        assert bridge.poll_inbox_file_for_lumina() == []


# ─── deliver_reply_to_inbox ──────────────────────────────────────────────────


class _FakeHistory:
    """Stand-in for ChatHistory so delivery never touches live JSONL/SQLite."""

    saved: list = []

    @classmethod
    def from_config(cls):
        return cls()

    def save(self, msg):
        type(self).saved.append(msg)

    def store_message(self, msg):
        type(self).saved.append(msg)


class TestDeliverReplyToInbox:
    def test_writes_envelope_into_inbox(self, bridge, monkeypatch):
        import skchat.history as _hist

        monkeypatch.setattr(_hist, "ChatHistory", _FakeHistory)

        bridge.deliver_reply_to_inbox(
            reply_text="hi from lumina",
            recipient=OPUS,
            thread_id="t-42",
            reply_to="m-1",
        )

        files = list(bridge.INBOX_PATH.glob("*.skc.json"))
        assert len(files) == 1
        env = json.loads(files[0].read_text())
        assert env["sender"] == LUMINA
        assert env["recipient"] == OPUS
        assert env["metadata"]["thread_id"] == "t-42"
        assert env["metadata"]["delivered_via"] == "local_bridge"
        # The reply text is carried inside the serialized ChatMessage payload.
        assert "hi from lumina" in env["payload"]["content"]

    def test_no_temp_file_left_behind(self, bridge, monkeypatch):
        import skchat.history as _hist

        monkeypatch.setattr(_hist, "ChatHistory", _FakeHistory)
        bridge.deliver_reply_to_inbox(reply_text="x", recipient=OPUS)
        # No leftover ".<name>.tmp" atomic-write artifacts.
        assert list(bridge.INBOX_PATH.glob(".*.tmp")) == []


# ─── end-to-end loopback: outbox → poll → reply lands in inbox ───────────────


class TestLoopbackRoundtrip:
    def test_outbox_message_produces_inbox_reply(self, bridge, monkeypatch):
        import skchat.history as _hist

        monkeypatch.setattr(_hist, "ChatHistory", _FakeHistory)

        # Opus sends to Lumina — lands in the local outbox (no Syncthing).
        _write_envelope(bridge.OUTBOX_PATH, recipient=LUMINA, sender=OPUS,
                        content="are you there?")

        # Bridge picks it up from the outbox.
        msgs = bridge.poll_outbox_for_lumina()
        assert len(msgs) == 1
        original = msgs[0]

        # Bridge delivers Lumina's reply back to the sender via the inbox.
        bridge.send_reply(original, "yes, I'm here")

        inbox_files = list(bridge.INBOX_PATH.glob("*.skc.json"))
        assert len(inbox_files) == 1
        env = json.loads(inbox_files[0].read_text())
        assert env["sender"] == LUMINA
        assert env["recipient"] == OPUS  # reply routed back to original sender
        assert "yes, I'm here" in env["payload"]["content"]
