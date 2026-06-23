"""Phase 1 typed-message contract — models, history mutators, proxy endpoints.

Covers the §5 Phase-1 AC + the Golden rule: a message with an UNKNOWN
``content_type`` still deserializes and carries a usable ``body``, so any
client renders the fallback (forward-compat for Phases 4-6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.history import ChatHistory
from skchat.models import ChatMessage, ContentType


# ---------------------------------------------------------------------------
# Models — content_type extensibility (the Golden rule)
# ---------------------------------------------------------------------------
class TestContentTypeExtensible:
    def test_known_enum_normalises_to_canonical(self) -> None:
        m = ChatMessage(sender="a@x", recipient="b@x", content="hi",
                         content_type=ContentType.PLAIN)
        assert m.content_type == "text/plain"

    def test_short_wire_form_normalises_inbound(self) -> None:
        m = ChatMessage(sender="a@x", recipient="b@x", content="hi",
                        content_type="text")
        assert m.content_type == "text/plain"
        m2 = ChatMessage(sender="a@x", recipient="b@x", content="hi",
                         content_type="markdown")
        assert m2.content_type == "text/markdown"

    def test_unknown_content_type_deserializes_and_keeps_body(self) -> None:
        """GOLDEN RULE: unknown type is preserved + body survives."""
        raw = (
            '{"sender":"a@x","recipient":"b@x","content":"Shared a location",'
            '"content_type":"application/skchat.location+json",'
            '"rich":{"lat":1.0,"lon":2.0}}'
        )
        m = ChatMessage.model_validate_json(raw)
        # Type preserved verbatim (not rejected, not coerced away).
        assert m.content_type == "application/skchat.location+json"
        # Body fallback is intact.
        assert m.content == "Shared a location"
        # Typed payload carried for capable clients.
        assert m.rich == {"lat": 1.0, "lon": 2.0}

    def test_unknown_type_round_trips_to_wire_unchanged(self) -> None:
        m = ChatMessage(sender="a@x", recipient="b@x", content="poll body",
                        content_type="application/skchat.poll+json",
                        rich={"options": ["yes", "no"]})
        wire = ContentType.to_wire(m.content_type)
        assert wire == "application/skchat.poll+json"

    def test_rich_defaults_none_for_text(self) -> None:
        m = ChatMessage(sender="a@x", recipient="b@x", content="hi")
        assert m.rich is None

    def test_legacy_json_without_new_fields_loads(self) -> None:
        """Back-compat: old serialized messages have none of the new fields."""
        m = ChatMessage.model_validate_json(
            '{"sender":"a@x","recipient":"b@x","content":"old"}'
        )
        assert m.rich is None
        assert m.edited_at is None
        assert m.edit_history is None
        assert m.receipts is None


# ---------------------------------------------------------------------------
# Models — reactions / edits / receipts behaviour
# ---------------------------------------------------------------------------
class TestMessageMutators:
    def _msg(self, **kw) -> ChatMessage:
        return ChatMessage(sender="a@x", recipient="b@x", content="hello", **kw)

    def test_set_and_clear_reaction(self) -> None:
        m = self._msg()
        assert m.set_reaction("👍", "b@x") is True
        assert m.set_reaction("👍", "b@x") is False  # dup
        assert m.reactions_map() == {"👍": ["b@x"]}
        assert m.clear_reaction("👍", "b@x") is True
        assert m.reactions_map() == {}

    def test_edit_appends_history_and_stamps(self) -> None:
        m = self._msg()
        m.apply_edit("hello (edited)")
        assert m.content == "hello (edited)"
        assert m.edited_at is not None
        assert m.edit_history and m.edit_history[0].body == "hello"

    def test_edit_window_enforced(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        m = self._msg(timestamp=old)
        with pytest.raises(ValueError, match="edit window"):
            m.apply_edit("too late")

    def test_edit_within_window_ok(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(hours=23)
        m = self._msg(timestamp=recent)
        m.apply_edit("just in time")
        assert m.content == "just in time"

    def test_record_receipt_idempotent(self) -> None:
        m = self._msg()
        assert m.record_receipt("delivered", "b@x") is True
        assert m.record_receipt("delivered", "b@x") is False
        assert m.record_receipt("read", "b@x") is True
        assert m.receipts.delivered == ["b@x"]
        assert m.receipts.read == ["b@x"]

    def test_full_contract_round_trip(self) -> None:
        m = self._msg(thread_id="t1", reply_to_id="r1",
                      content_type="application/skchat.poll+json",
                      rich={"q": "?"})
        m.set_reaction("🔥", "b@x")
        m.record_receipt("read", "b@x")
        m.apply_edit("hello v2")
        restored = ChatMessage.model_validate_json(m.model_dump_json())
        assert restored.thread_id == "t1"
        assert restored.reply_to_id == "r1"
        assert restored.content_type == "application/skchat.poll+json"
        assert restored.rich == {"q": "?"}
        assert restored.reactions_map() == {"🔥": ["b@x"]}
        assert restored.receipts.read == ["b@x"]
        assert restored.edited_at is not None
        assert restored.edit_history[0].body == "hello"


# ---------------------------------------------------------------------------
# History — JSONL-safe in-place mutation
# ---------------------------------------------------------------------------
class TestHistoryMutation:
    @pytest.fixture
    def hist(self, tmp_path) -> ChatHistory:
        return ChatHistory(store=None, history_dir=tmp_path / "history")

    def test_reaction_round_trip_persists(self, hist) -> None:
        m = hist.add_message("a@x", "b@x", "react to me")
        hist.set_reaction(m.id, "👍", "b@x")
        reloaded = hist.find_by_id(m.id)
        assert reloaded.reactions_map() == {"👍": ["b@x"]}
        hist.clear_reaction(m.id, "👍", "b@x")
        assert hist.find_by_id(m.id).reactions_map() == {}

    def test_edit_persists_and_appends_history(self, hist) -> None:
        m = hist.add_message("a@x", "b@x", "v1")
        hist.edit_message(m.id, "v2")
        reloaded = hist.find_by_id(m.id)
        assert reloaded.content == "v2"
        assert reloaded.edit_history[0].body == "v1"
        assert reloaded.edited_at is not None

    def test_edit_window_refused_at_history_layer(self, hist) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        m = ChatMessage(sender="a@x", recipient="b@x", content="old", timestamp=old)
        hist.save(m)
        with pytest.raises(ValueError):
            hist.edit_message(m.id, "nope")

    def test_receipt_persists(self, hist) -> None:
        m = hist.add_message("a@x", "b@x", "deliver me")
        hist.record_receipt(m.id, "delivered", "b@x")
        hist.record_receipt(m.id, "read", "b@x")
        reloaded = hist.find_by_id(m.id)
        assert reloaded.receipts.delivered == ["b@x"]
        assert reloaded.receipts.read == ["b@x"]

    def test_mutation_preserves_other_lines(self, hist) -> None:
        a = hist.add_message("a@x", "b@x", "first")
        b = hist.add_message("a@x", "b@x", "second")
        hist.set_reaction(b.id, "🎉", "b@x")
        # The untouched message is intact.
        assert hist.find_by_id(a.id).content == "first"
        assert hist.find_by_id(b.id).reactions_map() == {"🎉": ["b@x"]}

    def test_mutation_preserves_malformed_lines(self, hist, tmp_path) -> None:
        m = hist.add_message("a@x", "b@x", "good")
        # Inject a junk line that must survive the rewrite untouched.
        day = sorted((tmp_path / "history").glob("*.jsonl"))[0]
        with day.open("a") as fh:
            fh.write("THIS IS NOT JSON\n")
        hist.set_reaction(m.id, "👍", "b@x")
        text = day.read_text()
        assert "THIS IS NOT JSON" in text

    def test_missing_message_returns_none(self, hist) -> None:
        assert hist.set_reaction("no-such-id", "👍", "b@x") is None
        assert hist.edit_message("no-such-id", "x") is None
        assert hist.record_receipt("no-such-id", "read", "b@x") is None

    def test_thread_linkage_via_get_thread(self, hist) -> None:
        root = hist.add_message("a@x", "b@x", "root")
        reply = ChatMessage(sender="b@x", recipient="a@x", content="reply",
                            thread_id="thread-1", reply_to_id=root.id)
        hist.save(reply)
        also = ChatMessage(sender="a@x", recipient="b@x", content="follow",
                           thread_id="thread-1")
        hist.save(also)
        msgs = hist.get_thread("thread-1")
        assert [m.content for m in msgs] == ["reply", "follow"]
        assert msgs[0].reply_to_id == root.id


# ---------------------------------------------------------------------------
# daemon_proxy endpoints — the app-facing contract
# ---------------------------------------------------------------------------
class _StubBrain:
    def reply(self, user_text, history=None, sender="chef"):
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(daemon_proxy, "_BRAIN", _StubBrain())
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._hist = hist  # type: ignore[attr-defined]
    return c


class TestProxyContract:
    def test_conversation_returns_full_contract(self, client) -> None:
        client.post("/api/v1/send", json={"recipient": "lumina", "message": "hi"})
        msgs = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
        assert msgs
        m = msgs[0]
        for key in ("id", "conversation_id", "sender", "content_type", "body",
                    "rich", "ts", "reply_to_id", "thread_id", "edited_at",
                    "edit_history", "reactions", "receipts"):
            assert key in m, f"missing {key}"
        assert m["content_type"] == "markdown"  # short wire form
        assert m["body"] == "hi"

    def test_send_with_reply_and_thread_links(self, client) -> None:
        first = client.post("/api/v1/send",
                            json={"recipient": "lumina", "message": "parent"}).json()
        parent_id = first["id"]
        second = client.post("/api/v1/send", json={
            "recipient": "lumina", "message": "child",
            "reply_to_id": parent_id, "thread_id": "thread-X",
        }).json()
        # Lumina's reply links back to the user turn + inherits the thread.
        assert second["reply"]["reply_to_id"] == second["id"]
        assert second["reply"]["thread_id"] == "thread-X"
        # Thread endpoint returns the linked messages.
        thr = client.get("/api/v1/thread/thread-X").json()
        bodies = [m["body"] for m in thr["messages"]]
        assert "child" in bodies

    def test_react_add_remove_round_trip(self, client) -> None:
        sent = client.post("/api/v1/send",
                           json={"recipient": "lumina", "message": "react"}).json()
        mid = sent["id"]
        r = client.post("/api/v1/react", json={
            "conversation_id": daemon_proxy.LUMINA_ID, "message_id": mid,
            "emoji": "👍", "op": "add"})
        assert r.status_code == 200
        assert r.json()["message"]["reactions"] == {"👍": [daemon_proxy.OPERATOR_ID]}
        r2 = client.post("/api/v1/react", json={
            "message_id": mid, "emoji": "👍", "op": "remove"})
        assert r2.json()["message"]["reactions"] is None

    def test_edit_endpoint_appends_history(self, client) -> None:
        sent = client.post("/api/v1/send",
                           json={"recipient": "lumina", "message": "v1"}).json()
        mid = sent["id"]
        r = client.post("/api/v1/edit", json={"message_id": mid, "body": "v2"})
        assert r.status_code == 200
        msg = r.json()["message"]
        assert msg["body"] == "v2"
        assert msg["edited_at"] is not None
        assert msg["edit_history"][0]["body"] == "v1"

    def test_edit_out_of_window_is_403(self, client) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        m = ChatMessage(sender=daemon_proxy.OPERATOR_ID,
                        recipient=daemon_proxy.LUMINA_URI, content="old", timestamp=old)
        client._hist.save(m)
        r = client.post("/api/v1/edit", json={"message_id": m.id, "body": "late"})
        assert r.status_code == 403

    def test_receipt_endpoint_records(self, client) -> None:
        sent = client.post("/api/v1/send",
                           json={"recipient": "lumina", "message": "deliver"}).json()
        mid = sent["id"]
        r = client.post("/api/v1/receipt", json={
            "conversation_id": daemon_proxy.LUMINA_ID, "message_id": mid,
            "kind": "read", "sender": "chef@skworld.io"})
        assert r.status_code == 200
        assert r.json()["message"]["receipts"]["read"] == ["chef@skworld.io"]

    def test_react_missing_message_404(self, client) -> None:
        r = client.post("/api/v1/react",
                        json={"message_id": "nope", "emoji": "👍", "op": "add"})
        assert r.status_code == 404

    def test_unknown_content_type_renders_body_through_proxy(self, client) -> None:
        """End-to-end Golden rule: a typed message surfaces its body via the API."""
        m = ChatMessage(
            sender=daemon_proxy.LUMINA_URI, recipient=daemon_proxy.OPERATOR_ID,
            content="📍 Shared a location",
            content_type="application/skchat.location+json",
            rich={"lat": 1.0, "lon": 2.0})
        client._hist.save(m)
        msgs = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
        loc = [x for x in msgs if x["id"] == m.id][0]
        assert loc["content_type"] == "application/skchat.location+json"
        assert loc["body"] == "📍 Shared a location"
        assert loc["rich"] == {"lat": 1.0, "lon": 2.0}
