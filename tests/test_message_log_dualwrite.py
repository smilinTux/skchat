"""Task 2: ChatHistory.record_event dual-writes to the authoritative log.

Flag OFF (default) -> no-op (no log touched). Flag ON -> one log row per logical
message on the canonical conversation_id; the 1+N group fan-out copies collapse.
"""
from __future__ import annotations

from datetime import datetime, timezone

from skchat.history import ChatHistory
from skchat.message_log import MessageLog
from skchat.models import ChatMessage


def test_record_event_flag_off_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv("SKCHAT_MESSAGE_LOG", raising=False)
    h = ChatHistory(history_dir=tmp_path / "history")
    h.record_event(ChatMessage(sender="a", recipient="b", content="x"))
    assert not (tmp_path / "message_log.db").exists()  # flag off -> log untouched


def test_record_event_group_fanout_collapses_to_one_row(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_MESSAGE_LOG", "1")
    h = ChatHistory(history_dir=tmp_path / "history")
    ts = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    # canonical group event + a per-member copy (different id, member recipient,
    # SAME metadata.group_id) -> both map to group:g1 and dedup to one row.
    canonical = ChatMessage(
        sender="lumina", recipient="group:g1", content="team",
        metadata={"group_id": "g1"}, timestamp=ts,
    )
    member = ChatMessage(
        sender="lumina", recipient="chef", content="team",
        metadata={"group_id": "g1"}, timestamp=ts,
    )
    h.record_event(canonical)
    h.record_event(member)
    log = MessageLog(str(tmp_path / "message_log.db"))
    rows = log.read("group:g1")
    assert len(rows) == 1
    assert rows[0]["sender"] == "lumina" and rows[0]["content"] == "team"


def test_record_event_dm_maps_to_pair_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_MESSAGE_LOG", "1")
    h = ChatHistory(history_dir=tmp_path / "history")
    h.record_event(ChatMessage(sender="alice", recipient="bob", content="hi"))
    log = MessageLog(str(tmp_path / "message_log.db"))
    assert len(log.read("dm:alice|bob")) == 1
