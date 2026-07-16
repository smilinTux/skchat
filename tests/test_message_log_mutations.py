"""Task 5 core: a mutation (reaction/edit) updates the ONE logical message's
payload in the authoritative log, so log-sourced readers see it (fixes the old
"reaction reaches only one fan-out copy" and the read-cutover staleness)."""
from __future__ import annotations

from skchat.history import ChatHistory
from skchat.message_log import MessageLog
from skchat.models import ChatMessage


def test_reaction_reflected_in_log_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_MESSAGE_LOG", "1")
    h = ChatHistory(history_dir=tmp_path / "history")
    msg = ChatMessage(sender="lumina", recipient="group:g1", content="hi",
                      thread_id="g1", metadata={"group_id": "g1"})
    h.save(msg)          # store A copy so set_reaction can find_by_id it
    h.record_event(msg)  # authoritative log (send-time snapshot, no reaction yet)
    log = MessageLog(str(tmp_path / "message_log.db"))
    assert "thumbsup" not in (log.read("group:g1")[0]["payload"] or "")

    h.set_reaction(msg.id, "thumbsup", "chef")  # mutation -> _update_log_payload

    # the log payload now carries the reaction, so any log-sourced reader sees it
    payload = MessageLog(str(tmp_path / "message_log.db")).read("group:g1")[0]["payload"]
    assert "thumbsup" in (payload or "")


def test_edit_reflected_in_log_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_MESSAGE_LOG", "1")
    h = ChatHistory(history_dir=tmp_path / "history")
    msg = ChatMessage(sender="lumina", recipient="chef", content="original")
    h.save(msg)
    h.record_event(msg)
    h.edit_message(msg.id, "edited", enforce_window=False)
    payload = MessageLog(str(tmp_path / "message_log.db")).read("dm:chef|lumina")[0]["payload"]
    assert "edited" in (payload or "")
