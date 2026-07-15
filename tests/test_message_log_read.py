"""Task 3: read cutover — group_thread_messages serves from the authoritative
log when SKCHAT_MESSAGE_LOG is on (deduped, full payloads), else legacy."""
from __future__ import annotations

from skchat.history import ChatHistory
from skchat import daemon_proxy_groups as G
from skchat.models import ChatMessage


def _mk_group_msg(gid, content, sender="lumina"):
    return ChatMessage(sender=sender, recipient=f"group:{gid}", content=content,
                       thread_id=gid, metadata={"group_id": gid})


def test_group_read_from_log_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_MESSAGE_LOG", "1")
    h = ChatHistory(history_dir=tmp_path / "history")
    h.record_event(_mk_group_msg("g1", "first"))
    h.record_event(_mk_group_msg("g1", "second"))
    msgs = G.group_thread_messages(h, "g1", limit=50)
    assert [m.content for m in msgs] == ["first", "second"]  # seq-ordered from log


def test_group_read_falls_back_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv("SKCHAT_MESSAGE_LOG", raising=False)
    h = ChatHistory(history_dir=tmp_path / "history")
    # legacy path reads JSONL canonical group copies
    h.save(_mk_group_msg("g2", "legacy"))
    msgs = G.group_thread_messages(h, "g2", limit=50)
    assert [m.content for m in msgs] == ["legacy"]
