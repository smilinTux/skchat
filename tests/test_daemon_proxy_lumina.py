"""Tests for the Lumina chat surface in ``skchat.daemon_proxy``.

Contract the Flutter app depends on:
  * Lumina is ALWAYS present + first in /api/v1/peers and /api/v1/conversations,
    flagged ``is_agent`` and ``is_online`` while the operator is in-app.
  * POST /api/v1/send to Lumina persists the operator's message, invokes her
    brain (mocked offline here), persists + returns her reply.
  * The thread reads back through /api/v1/conversations + /api/v1/inbox +
    /api/v1/conversations/{peer_id}, ordered oldest-first with timestamps.

The qwen3.6 HTTP backend is never touched — a stub ``LuminaBrain`` is injected.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy


class _StubBrain:
    """Offline stand-in for the real LuminaBrain.reply (no qwen HTTP)."""

    def __init__(self):
        self.calls: list[dict] = []

    def reply(self, user_text, history=None, sender="chef"):
        self.calls.append({"text": user_text, "history": history or []})
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate history to a tmp JSONL dir.
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)

    # Inject the offline brain so no backend call ever happens.
    brain = _StubBrain()
    monkeypatch.setattr(daemon_proxy, "_BRAIN", brain)

    # Don't enrich with the operator's real ~/.skcapstone/peers in tests.
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._brain = brain  # type: ignore[attr-defined]
    c._hist = hist  # type: ignore[attr-defined]
    return c


def test_lumina_always_present_in_peers_and_conversations(client):
    for path in ("/api/v1/peers", "/api/v1/conversations"):
        r = client.get(path)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and body, f"{path} empty"
        first = body[0]
        assert first["peer_id"] == daemon_proxy.LUMINA_ID
        assert first["display_name"] == "Lumina"
        assert first["is_agent"] is True
        assert first["is_online"] is True
        # Full app conversation contract present.
        for key in ("last_message", "last_message_time", "soul_fingerprint",
                    "unread_count", "is_group", "member_count", "avatar_url"):
            assert key in first


def test_send_to_lumina_persists_pair_and_returns_reply(client):
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "hi there"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["recipient"] == daemon_proxy.LUMINA_ID
    # Her reply is returned, non-empty, in her voice (stub), flagged agent.
    reply = body["reply"]
    assert reply["content"] == "Lumina hears you: hi there"
    assert reply["is_agent"] is True
    assert reply["is_outbound"] is False
    assert reply["reply_to_id"] == body["id"]
    # The brain was actually invoked.
    assert client._brain.calls and client._brain.calls[0]["text"] == "hi there"

    # Thread reads back: the operator message + Lumina's reply, oldest-first.
    hist = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    assert [m["content"] for m in hist] == ["hi there", "Lumina hears you: hi there"]
    assert hist[0]["is_outbound"] is True and hist[0]["is_agent"] is False
    assert hist[1]["is_outbound"] is False and hist[1]["is_agent"] is True
    assert all("timestamp" in m for m in hist)

    # Inbox carries the same thread (app groups by peer_id).
    inbox = client.get("/api/v1/inbox").json()["messages"]
    assert [m["content"] for m in inbox] == ["hi there", "Lumina hears you: hi there"]
    assert all(m["peer_id"] == daemon_proxy.LUMINA_ID for m in inbox)

    # Conversation list preview reflects the latest message.
    convo = client.get("/api/v1/conversations").json()[0]
    assert convo["last_message"] == "Lumina hears you: hi there"


def test_send_empty_message_is_400(client):
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "   "})
    assert r.status_code == 400


def test_group_faninto_is_excluded_but_threaded_dm_is_kept(client, monkeypatch):
    """Regression for bughunt defect #2 (over-broad thread_id exclusion).

    ``_lumina_messages()`` must drop a real GROUP fan-out copy addressed to
    Lumina (``metadata.group_id`` set, or ``thread_id`` matching a
    persisted group) while KEEPING a genuine 1:1 DM that merely carries a
    client-supplied ``thread_id`` for in-chat threading. Previously any
    truthy ``thread_id`` was treated as "must be a group message" and both
    cases were dropped.
    """
    from skchat import daemon_proxy_groups as G
    from skchat.models import ChatMessage

    # Isolate the group store so `load_group` doesn't touch a real ~/.skchat.
    monkeypatch.setattr(G, "_GROUPS_DIR", client._hist._history_dir.parent / "groups")

    hist = client._hist

    # (a) A genuine GROUP fan-out copy: persisted group id used as thread_id,
    # plus a per-member copy addressed straight to Lumina. Must be excluded.
    from skchat.group import GroupChat

    group = GroupChat(
        id="grp-real-123",
        name="Penguins",
        created_by="capauth:chef@skworld.io",
    )
    G.save_group(group)

    group_copy = ChatMessage(
        sender="capauth:chef@skworld.io",
        recipient=daemon_proxy.LUMINA_URI,
        content="group fan-out copy",
        thread_id="grp-real-123",
        metadata={"group_id": "grp-real-123"},
    )
    hist.save(group_copy)

    # (b) A genuine 1:1 DM that the client threaded with its own thread_id.
    # No group exists with this id -> must be KEPT, not dropped.
    dm_reply = ChatMessage(
        sender="capauth:chef@skworld.io",
        recipient=daemon_proxy.LUMINA_URI,
        content="a threaded 1:1 reply",
        thread_id="reply-thread-abc",
    )
    hist.save(dm_reply)

    lumina_reply = ChatMessage(
        sender=daemon_proxy.LUMINA_URI,
        recipient="capauth:chef@skworld.io",
        content="my threaded 1:1 answer",
        thread_id="reply-thread-abc",
    )
    hist.save(lumina_reply)

    msgs = daemon_proxy._lumina_messages()
    contents = [m["content"] for m in msgs]

    assert "group fan-out copy" not in contents
    assert "a threaded 1:1 reply" in contents
    assert "my threaded 1:1 answer" in contents

    # Same contract holds through the real HTTP surface.
    inbox = client.get("/api/v1/inbox").json()["messages"]
    inbox_contents = [m["content"] for m in inbox]
    assert "group fan-out copy" not in inbox_contents
    assert "a threaded 1:1 reply" in inbox_contents
    assert "my threaded 1:1 answer" in inbox_contents


def test_brain_failure_persists_graceful_reply_not_500(client, monkeypatch):
    class _BoomBrain:
        def reply(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(daemon_proxy, "_BRAIN", _BoomBrain())
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "you there?"})
    assert r.status_code == 200
    reply = r.json()["reply"]
    assert reply["content"] and "thinking failed" in reply["content"].lower()
    # Both turns are still persisted.
    hist = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    assert len(hist) == 2 and hist[0]["content"] == "you there?"


def test_react_control_frame_short_circuits_brain(client):
    """P0.3b: a ``__REACT__`` control frame applies the reaction best-effort,
    never invokes the brain, and returns ``control: 'reaction'``."""
    import json as _json

    # A real message to react to (this first send does hit the brain).
    sent = client.post("/api/v1/send", json={"recipient": "lumina", "message": "hi"}).json()
    target_id = sent["id"]
    client._brain.calls.clear()

    frame = "__REACT__:" + _json.dumps({"target_id": target_id, "emoji": "👍", "op": "add"})
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": frame})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "control": "reaction"}
    # Brain NOT invoked, no reply.
    assert client._brain.calls == []
    assert "reply" not in r.json()
    # The reaction was applied best-effort (not persisted as a chat turn).
    msg = client._hist.find_by_id(target_id)
    assert "👍" in (msg.reactions_map() or {})
    # No stray control-frame message appears in the thread.
    hist = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    assert all("__REACT__" not in m["content"] for m in hist)


def test_edit_control_frame_short_circuits_brain(client):
    """P0.3b: an ``__EDIT__`` control frame edits the target best-effort,
    never invokes the brain, and returns ``control: 'edit'``."""
    import json as _json

    sent = client.post("/api/v1/send", json={"recipient": "lumina", "message": "typo"}).json()
    target_id = sent["id"]
    client._brain.calls.clear()

    frame = "__EDIT__:" + _json.dumps({"target_id": target_id, "body": "fixed"})
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": frame})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "control": "edit"}
    # Brain NOT invoked, no reply.
    assert client._brain.calls == []
    assert "reply" not in r.json()
    # The edit was applied best-effort.
    msg = client._hist.find_by_id(target_id)
    assert msg.content == "fixed"
    # No stray control-frame message appears in the thread.
    hist = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    assert all("__EDIT__" not in m["content"] for m in hist)
