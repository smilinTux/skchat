"""Tests for the SKChat transport bridge (skchat.transport.ChatTransport).

Covers:
- send_message with mocked SKComms (success + failure)
- poll_inbox with mocked envelopes
- send_and_store convenience method
- Graceful degradation when SKComms operations fail
- Direct file inbox polling (_poll_file_inbox)
- Loopback delivery when sender == receiver
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skchat import transport as transport_module
from skchat.models import ChatMessage, DeliveryStatus
from skchat.transport import ChatTransport, _write_local_loopback


@pytest.fixture
def mock_skcomms():
    """Create a mock SKComms instance."""
    comm = MagicMock()
    comm.send.return_value = MagicMock(
        delivered=True,
        successful_transport="syncthing",
    )
    comm.receive.return_value = []
    return comm


@pytest.fixture
def mock_history():
    """Create a mock ChatHistory."""
    history = MagicMock()
    history.store_message.return_value = "mem-123"
    return history


@pytest.fixture
def transport(mock_skcomms, mock_history):
    """Create a ChatTransport with mocked dependencies."""
    return ChatTransport(
        skcomms=mock_skcomms,
        history=mock_history,
        identity="capauth:test@skchat",
    )


class TestSendMessage:
    """Tests for ChatTransport.send_message()."""

    def test_send_success(self, transport, mock_skcomms, mock_history):
        """Message is sent via SKComms and stored in history."""
        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:lumina@skworld",
            content="Hello Lumina!",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is True
        assert result["recipient"] == "capauth:lumina@skworld"
        mock_skcomms.send.assert_called_once()
        mock_history.store_message.assert_called_once()

    def test_send_failure_stores_as_failed(self, transport, mock_skcomms, mock_history):
        """Failed delivery still stores the message with FAILED status."""
        mock_skcomms.send.return_value = MagicMock(
            delivered=False,
            successful_transport=None,
        )

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:nobody@nowhere",
            content="This should fail",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is False
        mock_history.store_message.assert_called_once()
        stored_msg = mock_history.store_message.call_args[0][0]
        assert stored_msg.delivery_status == DeliveryStatus.FAILED

    def test_send_exception_returns_error(self, transport, mock_skcomms, mock_history):
        """SKComms exception is caught and reported gracefully."""
        mock_skcomms.send.side_effect = ConnectionError("Transport unreachable")

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:lumina@skworld",
            content="Oops",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is False
        assert "error" in result
        assert "Transport unreachable" in result["error"]


class TestPollInbox:
    """Tests for ChatTransport.poll_inbox()."""

    def test_poll_empty_inbox(self, transport, mock_skcomms):
        """Empty inbox returns empty list."""
        mock_skcomms.receive.return_value = []

        messages = transport.poll_inbox()

        assert messages == []

    def test_poll_with_message(self, transport, mock_skcomms, mock_history):
        """Valid envelope is parsed and stored as ChatMessage."""
        msg_data = ChatMessage(
            sender="capauth:opus@smilintux",
            recipient="capauth:test@skchat",
            content="Hello from Opus!",
        )

        envelope = MagicMock()
        envelope.payload.content = msg_data.model_dump_json()
        mock_skcomms.receive.return_value = [envelope]

        messages = transport.poll_inbox()

        assert len(messages) == 1
        assert messages[0].content == "Hello from Opus!"
        assert messages[0].delivery_status == DeliveryStatus.DELIVERED
        mock_history.store_message.assert_called_once()

    def test_poll_skips_invalid_envelope(self, transport, mock_skcomms):
        """Invalid envelope payloads are silently skipped."""
        envelope = MagicMock()
        envelope.payload.content = "not valid json {{"
        mock_skcomms.receive.return_value = [envelope]

        messages = transport.poll_inbox()

        assert messages == []

    def test_poll_receive_failure(self, transport, mock_skcomms):
        """SKComms receive failure returns empty list."""
        mock_skcomms.receive.side_effect = RuntimeError("Network error")

        messages = transport.poll_inbox()

        assert messages == []


class TestSendAndStore:
    """Tests for ChatTransport.send_and_store() convenience method."""

    def test_compose_and_deliver(self, transport, mock_skcomms, mock_history):
        """send_and_store composes a ChatMessage and delivers it."""
        result = transport.send_and_store(
            recipient="capauth:lumina@skworld",
            content="Quick message",
            thread_id="thread-abc",
        )

        assert result["delivered"] is True
        mock_skcomms.send.assert_called_once()

        call_kwargs = mock_skcomms.send.call_args
        assert "capauth:lumina@skworld" in str(call_kwargs)


# ---------------------------------------------------------------------------
# File inbox polling tests
# ---------------------------------------------------------------------------


def _make_transport(tmp_path: Path, identity: str = "capauth:test@skchat") -> tuple:
    """Create a ChatTransport wired to a temp file inbox and mock history."""
    mock_skcomms = MagicMock()
    mock_skcomms.receive.return_value = []
    mock_history = MagicMock()
    mock_history.store_message.return_value = "mem-001"

    ct = ChatTransport(
        skcomms=mock_skcomms,
        history=mock_history,
        identity=identity,
    )
    # Override the file inbox root to use a tmp dir — avoids touching ~/.skcomms
    ct._file_inbox_root = tmp_path / "transport" / "file" / "inbox"
    return ct, mock_skcomms, mock_history


class TestFileInboxPoll:
    """Tests for ChatTransport._poll_file_inbox()."""

    def test_poll_file_inbox_no_dir(self, tmp_path):
        """Returns [] gracefully when the fingerprint inbox dir doesn't exist."""
        ct, _, _ = _make_transport(tmp_path)
        # Override fingerprint so we control the path
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        messages = ct._poll_file_inbox()

        assert messages == []

    def test_poll_file_inbox_empty_dir(self, tmp_path):
        """Returns [] when the inbox dir exists but is empty."""
        ct, _, _ = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        assert ct._poll_file_inbox() == []

    def test_poll_file_inbox_detects_message(self, tmp_path):
        """Valid ChatMessage JSON envelope is detected, parsed, and returned."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:test@skchat",
            content="Hello from file inbox!",
        )
        envelope = {
            "skcomms_version": "1.0.0",
            "envelope_id": "aabbccdd",
            "sender": msg.sender,
            "recipient": msg.recipient,
            "payload": {"content": msg.model_dump_json(), "content_type": "text"},
        }
        (inbox / "aabbccdd.skc.json").write_text(json.dumps(envelope), encoding="utf-8")

        messages = ct._poll_file_inbox()

        assert len(messages) == 1
        assert messages[0].content == "Hello from file inbox!"
        assert messages[0].delivery_status == DeliveryStatus.DELIVERED
        mock_history.store_message.assert_called_once()

    def test_poll_file_inbox_archives_processed_file(self, tmp_path):
        """Processed files are moved to the archive directory."""
        ct, _, _ = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:bob@skworld.io",
            recipient="capauth:test@skchat",
            content="archive me",
        )
        envelope = {
            "envelope_id": "ee112233",
            "payload": {"content": msg.model_dump_json()},
        }
        env_file = inbox / "ee112233.skc.json"
        env_file.write_text(json.dumps(envelope), encoding="utf-8")

        ct._poll_file_inbox()

        # Original file should be gone
        assert not env_file.exists()
        # Archive dir should contain it
        archive_dir = ct._file_inbox_root / "archive" / "TESTFP"  # type: ignore[attr-defined]
        assert archive_dir.exists()
        archived = list(archive_dir.glob("*.skc.json"))
        assert len(archived) == 1

    def test_poll_file_inbox_skips_dotfiles(self, tmp_path):
        """Temp files starting with '.' are not processed."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)
        (inbox / ".in_progress.skc.json").write_text("{}", encoding="utf-8")

        ct._poll_file_inbox()

        mock_history.store_message.assert_not_called()

    def test_poll_file_inbox_skips_invalid_json(self, tmp_path):
        """Invalid JSON files are archived without crashing."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)
        (inbox / "bad.skc.json").write_text("not json {{{{", encoding="utf-8")

        messages = ct._poll_file_inbox()

        assert messages == []
        mock_history.store_message.assert_not_called()
        # Original bad file should still be archived (not left behind)
        assert not (inbox / "bad.skc.json").exists()

    def test_poll_inbox_includes_file_inbox_results(self, tmp_path):
        """poll_inbox() combines SKComms envelopes + direct file inbox results."""
        ct, mock_skcomms, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        # Put one message in the file inbox
        msg = ChatMessage(
            sender="capauth:peer@skworld.io",
            recipient="capauth:test@skchat",
            content="via file inbox",
        )
        envelope = {
            "envelope_id": "ff998877",
            "payload": {"content": msg.model_dump_json()},
        }
        (inbox / "ff998877.skc.json").write_text(json.dumps(envelope), encoding="utf-8")

        # SKComms receive returns nothing
        mock_skcomms.receive.return_value = []

        messages = ct.poll_inbox()

        assert len(messages) == 1
        assert messages[0].content == "via file inbox"


class TestFileInboxStoreFailureIsNotLost:
    """Regression tests: a transient store_message() failure must not lose

    the message. The source .skc.json file must stay in place (not be
    archived) so it is retried on the next poll, and the original content
    must never be dropped or mangled into a fallback wrapper.
    """

    def test_store_failure_leaves_source_file_and_returns_no_message(self, tmp_path):
        """store_message() raising means: no archive, no returned message."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:carol@skworld.io",
            recipient="capauth:test@skchat",
            content="do not lose me",
        )
        envelope = {
            "envelope_id": "deadbeef",
            "payload": {"content": msg.model_dump_json()},
        }
        env_file = inbox / "deadbeef.skc.json"
        env_file.write_text(json.dumps(envelope), encoding="utf-8")

        mock_history.store_message.side_effect = RuntimeError("transient db error")

        messages = ct._poll_file_inbox()

        # Nothing was successfully stored, so nothing should be reported back.
        assert messages == []
        # The source file must still be present — not archived, not deleted.
        assert env_file.exists()
        archive_dir = ct._file_inbox_root / "archive" / "TESTFP"  # type: ignore[attr-defined]
        assert not archive_dir.exists() or not list(archive_dir.glob("*.skc.json"))

    def test_store_failure_then_success_recovers_original_content_on_retry(self, tmp_path):
        """After a transient failure, the NEXT poll retries the same file and

        stores the original (un-mangled) content — proving no silent loss and
        no garbled-JSON re-wrap of the payload.
        """
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:carol@skworld.io",
            recipient="capauth:test@skchat",
            content="do not lose me",
        )
        envelope = {
            "envelope_id": "deadbeef",
            "payload": {"content": msg.model_dump_json()},
        }
        env_file = inbox / "deadbeef.skc.json"
        env_file.write_text(json.dumps(envelope), encoding="utf-8")

        # Fail exactly once, then succeed — simulates a transient error.
        mock_history.store_message.side_effect = [RuntimeError("transient db error"), "mem-999"]

        first_pass = ct._poll_file_inbox()
        assert first_pass == []
        assert env_file.exists()  # retained for retry

        second_pass = ct._poll_file_inbox()

        assert len(second_pass) == 1
        # Content must be the ORIGINAL message content, not a garbled
        # re-wrap of the whole envelope/payload JSON as free text.
        assert second_pass[0].content == "do not lose me"
        assert second_pass[0].sender == "capauth:carol@skworld.io"
        assert second_pass[0].delivery_status == DeliveryStatus.DELIVERED

        # Now that storage succeeded, the file is archived exactly once.
        assert not env_file.exists()
        archive_dir = ct._file_inbox_root / "archive" / "TESTFP"  # type: ignore[attr-defined]
        archived = list(archive_dir.glob("*.skc.json"))
        assert len(archived) == 1


# ---------------------------------------------------------------------------
# Loopback send tests
# ---------------------------------------------------------------------------


class TestLoopback:
    """Tests for sender == receiver loopback delivery."""

    def test_loopback_send_does_not_call_skcomms(self, tmp_path):
        """Self-addressed messages bypass SKComms.send()."""
        ct, mock_skcomms, mock_history = _make_transport(
            tmp_path, identity="capauth:self@skworld.io"
        )
        ct._get_own_fingerprint = lambda: "SELFTEST"  # type: ignore[method-assign]

        msg = ChatMessage(
            sender="capauth:self@skworld.io",
            recipient="capauth:self@skworld.io",
            content="self-message",
        )

        result = ct.send_message(msg)

        assert result["delivered"] is True
        assert result["transport"] == "file"
        mock_skcomms.send.assert_not_called()

    def test_loopback_send_writes_to_file_inbox(self, tmp_path):
        """Self-addressed message creates a .skc.json file in own inbox dir."""
        ct, _, _ = _make_transport(tmp_path, identity="capauth:self@skworld.io")
        ct._get_own_fingerprint = lambda: "SELFTEST"  # type: ignore[method-assign]

        msg = ChatMessage(
            sender="capauth:self@skworld.io",
            recipient="capauth:self@skworld.io",
            content="persisted via file",
        )

        ct.send_message(msg)

        inbox = ct._file_inbox_root / "SELFTEST"  # type: ignore[attr-defined]
        files = list(inbox.glob("*.skc.json"))
        assert len(files) == 1

    def test_loopback_message_is_recoverable_on_next_poll(self, tmp_path):
        """Send-then-poll roundtrip works end-to-end for loopback."""
        ct, _, mock_history = _make_transport(tmp_path, identity="capauth:self@skworld.io")
        ct._get_own_fingerprint = lambda: "SELFTEST"  # type: ignore[method-assign]

        # Reset call count to track history writes from poll separately
        msg = ChatMessage(
            sender="capauth:self@skworld.io",
            recipient="capauth:self@skworld.io",
            content="round-trip test",
        )
        ct.send_message(msg)

        # Now poll — should find the written file
        mock_history.reset_mock()
        messages = ct._poll_file_inbox()

        assert len(messages) == 1
        assert messages[0].content == "round-trip test"
        mock_history.store_message.assert_called_once()

    def test_get_own_fingerprint_fallback_slug(self, tmp_path):
        """Falls back to identity slug when no ~/.skcomms/config.yml fingerprint."""
        ct, _, _ = _make_transport(tmp_path, identity="capauth:opus@skworld.io")
        # Point config to a non-existent path to force fallback
        with patch("builtins.open", side_effect=FileNotFoundError):
            fp = ct._get_own_fingerprint()

        assert "@" not in fp  # slug sanitizes @ → _at_
        assert fp  # non-empty


# ---------------------------------------------------------------------------
# QA additions
# ---------------------------------------------------------------------------


class TestExtractPayload:
    """_extract_payload normalises both object envelopes and raw dicts."""

    def test_object_envelope_with_payload(self):
        env = MagicMock()
        env.payload.content = "hello"
        assert ChatTransport._extract_payload(env) == "hello"

    def test_dict_envelope(self):
        env = {"payload": {"content": "from dict"}}
        assert ChatTransport._extract_payload(env) == "from dict"

    def test_dict_envelope_non_dict_payload(self):
        env = {"payload": "raw string"}
        assert ChatTransport._extract_payload(env) == "raw string"

    def test_none_when_no_payload(self):
        # A plain object with no payload attribute and not a dict → None.
        assert ChatTransport._extract_payload(object()) is None


class TestSendTypingIndicator:
    def test_typing_indicator_sent_as_heartbeat(self, transport, mock_skcomms):
        transport.send_typing_indicator("capauth:lumina@skworld", thread_id="t1")
        mock_skcomms.send.assert_called_once()
        kwargs = mock_skcomms.send.call_args.kwargs
        assert kwargs["recipient"] == "capauth:lumina@skworld"

    def test_typing_indicator_swallows_errors(self, transport, mock_skcomms):
        """A failed typing send must never raise (it's best-effort)."""
        mock_skcomms.send.side_effect = ConnectionError("down")
        # No exception should escape.
        transport.send_typing_indicator("capauth:lumina@skworld")


class TestHandleHeartbeat:
    def test_no_presence_cache_is_noop(self, mock_skcomms, mock_history):
        ct = ChatTransport(
            skcomms=mock_skcomms, history=mock_history, identity="capauth:me@x"
        )  # no presence_cache
        # Should silently return without error.
        ct._handle_heartbeat(MagicMock())

    def test_typing_heartbeat_records_typing(self, mock_skcomms, mock_history):
        from skchat.presence import PresenceCache, PresenceIndicator, PresenceState

        cache = MagicMock(spec=PresenceCache)
        ct = ChatTransport(
            skcomms=mock_skcomms,
            history=mock_history,
            identity="capauth:me@x",
            presence_cache=cache,
        )
        ind = PresenceIndicator(identity_uri="capauth:peer@x", state=PresenceState.TYPING)
        env = MagicMock()
        env.payload.content = ind.model_dump_json()
        ct._handle_heartbeat(env)
        cache.set_typing.assert_called_once_with("capauth:peer@x", True)

    def test_non_typing_heartbeat_clears_typing(self, mock_skcomms, mock_history):
        from skchat.presence import PresenceCache, PresenceIndicator, PresenceState

        cache = MagicMock(spec=PresenceCache)
        ct = ChatTransport(
            skcomms=mock_skcomms,
            history=mock_history,
            identity="capauth:me@x",
            presence_cache=cache,
        )
        ind = PresenceIndicator(identity_uri="capauth:peer@x", state=PresenceState.ONLINE)
        env = MagicMock()
        env.payload.content = ind.model_dump_json()
        ct._handle_heartbeat(env)
        cache.set_typing.assert_called_once_with("capauth:peer@x", False)


class TestFromConfig:
    def test_from_config_builds_transport(self, mock_skcomms, mock_history):
        ct = ChatTransport.from_config(
            skcomms=mock_skcomms, history=mock_history, identity="capauth:x@y"
        )
        assert isinstance(ct, ChatTransport)
        assert ct.identity == "capauth:x@y"


class TestFileInboxRawFallback:
    def test_raw_chatmessage_json_without_envelope(self, tmp_path):
        """A file that is a bare ChatMessage JSON (no envelope wrapper) is read."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]
        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:peer@skworld.io",
            recipient="capauth:test@skchat",
            content="raw bare msg",
        )
        # Write the ChatMessage JSON directly (no {"payload": ...} wrapper).
        (inbox / "raw.skc.json").write_text(msg.model_dump_json(), encoding="utf-8")

        messages = ct._poll_file_inbox()
        assert len(messages) == 1
        assert messages[0].content == "raw bare msg"


# ---------------------------------------------------------------------------
# (A) Local loopback envelope tests — _write_local_loopback()
# ---------------------------------------------------------------------------


class TestWriteLocalLoopback:
    """Tests for the module-level _write_local_loopback() helper.

    A loopback send to a local peer (e.g. lumina@skworld.io) writes an
    envelope into the ~/.skcomms/outbox/ dir with
    metadata.delivered_via == 'local_loopback'.  Each test redirects the
    module-level _LOCAL_OUTBOX constant to a tmp dir so the real
    ~/.skcomms/outbox is never touched.
    """

    def test_loopback_writes_envelope_with_delivered_via(self, tmp_path, monkeypatch):
        """Writes exactly one .skc.json envelope tagged local_loopback."""
        outbox = tmp_path / "outbox"
        monkeypatch.setattr(transport_module, "_LOCAL_OUTBOX", outbox)

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="lumina@skworld.io",  # a member of _LOCAL_PEERS
            content="loopback to lumina",
        )

        _write_local_loopback(msg)

        files = list(outbox.glob("*.skc.json"))
        assert len(files) == 1

        envelope = json.loads(files[0].read_text(encoding="utf-8"))
        # The defining field/dir names verified against transport.py.
        assert envelope["metadata"]["delivered_via"] == "local_loopback"
        assert envelope["sender"] == "capauth:test@skchat"
        assert envelope["recipient"] == "lumina@skworld.io"
        assert envelope["payload"]["content_type"] == "text"
        assert envelope["payload"]["encrypted"] is False

        # payload.content is the ChatMessage JSON — round-trips cleanly.
        inner = ChatMessage.model_validate_json(envelope["payload"]["content"])
        assert inner.content == "loopback to lumina"
        assert inner.recipient == "lumina@skworld.io"

    def test_loopback_leaves_no_tmp_files(self, tmp_path, monkeypatch):
        """Atomic tmp->rename leaves only the final file, no .tmp residue."""
        outbox = tmp_path / "outbox"
        monkeypatch.setattr(transport_module, "_LOCAL_OUTBOX", outbox)

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="lumina@skworld.io",
            content="atomic check",
        )
        _write_local_loopback(msg)

        # No leftover dot-prefixed temp files (atomic write completed).
        all_entries = list(outbox.iterdir())
        assert all(not e.name.startswith(".") for e in all_entries)
        assert len([e for e in all_entries if e.suffix == ".json"]) == 1

    def test_send_message_to_local_peer_triggers_loopback(self, tmp_path, monkeypatch):
        """send_message() to a _LOCAL_PEERS recipient writes a loopback copy."""
        outbox = tmp_path / "outbox"
        monkeypatch.setattr(transport_module, "_LOCAL_OUTBOX", outbox)

        ct, mock_skcomms, _ = _make_transport(tmp_path, identity="capauth:test@skchat")

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="lumina@skworld.io",  # in _LOCAL_PEERS but != identity
            content="hi lumina",
        )
        ct.send_message(msg)

        # A loopback envelope landed in the outbox...
        files = list(outbox.glob("*.skc.json"))
        assert len(files) == 1
        env = json.loads(files[0].read_text(encoding="utf-8"))
        assert env["metadata"]["delivered_via"] == "local_loopback"
        # ...and the normal SKComms send still happened (loopback is additive).
        mock_skcomms.send.assert_called_once()


# ---------------------------------------------------------------------------
# (B) Concurrency / race tests — concurrent writers + single-cycle drain
# ---------------------------------------------------------------------------


class TestConcurrentWriteAndPollRace:
    """~10 concurrent writers + the file poller must drop NO messages, and a
    backlog of ~100 pending files must drain in a single poll cycle.

    Writers use _write_local_loopback's atomic tmp->rename discipline (mirrored
    here via the same envelope shape) writing into the per-fingerprint file
    inbox that _poll_file_inbox() drains.
    """

    @staticmethod
    def _write_inbox_envelope(inbox: Path, idx: int) -> None:
        """Atomically write one ChatMessage envelope into the inbox dir."""
        msg = ChatMessage(
            sender="capauth:peer@skworld.io",
            recipient="capauth:test@skchat",
            content=f"msg-{idx}",
        )
        envelope = {
            "skcomms_version": "1.0.0",
            "envelope_id": f"env{idx:05d}",
            "sender": msg.sender,
            "recipient": msg.recipient,
            "payload": {"content": msg.model_dump_json(), "content_type": "text"},
        }
        filename = f"env{idx:05d}.skc.json"
        target = inbox / filename
        tmp = inbox / f".{filename}.tmp"
        # Atomic tmp->rename so the poller never sees a partial file.
        tmp.write_bytes(json.dumps(envelope).encode("utf-8"))
        tmp.rename(target)

    def test_concurrent_writers_drop_no_messages(self, tmp_path):
        """~10 concurrent writers race the poller; every message is delivered once."""
        import threading

        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "RACEFP"  # type: ignore[method-assign]
        inbox = ct._file_inbox_root / "RACEFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        n_writers = 10
        per_writer = 10
        total = n_writers * per_writer

        # Barrier releases all parties at once: writers + poller + main thread.
        start = threading.Barrier(n_writers + 2)
        collected: list[ChatMessage] = []
        stop = threading.Event()

        def writer(w: int) -> None:
            start.wait()
            for j in range(per_writer):
                self._write_inbox_envelope(inbox, w * per_writer + j)

        def poller() -> None:
            start.wait()
            # Poll repeatedly while writers run, then a final drain after stop.
            while not stop.is_set():
                collected.extend(ct._poll_file_inbox())
            collected.extend(ct._poll_file_inbox())

        writer_threads = [threading.Thread(target=writer, args=(w,)) for w in range(n_writers)]
        poll_thread = threading.Thread(target=poller)
        poll_thread.start()
        for t in writer_threads:
            t.start()
        start.wait()  # release everyone at once

        for t in writer_threads:
            t.join()
        stop.set()
        poll_thread.join()

        # No message dropped: exactly `total` delivered, each content unique.
        assert len(collected) == total
        contents = {m.content for m in collected}
        assert contents == {f"msg-{i}" for i in range(total)}
        # Each delivered message was stored in history exactly once.
        assert mock_history.store_message.call_count == total

    def test_backlog_drains_in_one_poll_cycle(self, tmp_path):
        """~100 pending files all drain in a SINGLE _poll_file_inbox() call."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "DRAINFP"  # type: ignore[method-assign]
        inbox = ct._file_inbox_root / "DRAINFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        total = 100
        for i in range(total):
            self._write_inbox_envelope(inbox, i)

        # One poll cycle drains the whole backlog.
        messages = ct._poll_file_inbox()

        assert len(messages) == total
        assert {m.content for m in messages} == {f"msg-{i}" for i in range(total)}
        # Inbox is empty afterward (all archived) — a second poll yields nothing.
        assert ct._poll_file_inbox() == []
        assert list(inbox.glob("*.skc.json")) == []
