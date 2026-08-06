"""Cross-device reply-quote wire threading (card 5a19f848).

The reply-quote preview (quoted_text/quoted_sender/quoted_id) is denormalized
INTO the reply so a sibling/recipient device that never decrypted the original
still renders the quote. These tests prove:

* ``api_send`` round-trips the quoted_* fields through persistence into the
  served ``_msg_to_app`` output (the SAME hop reply_to_id already travels), and
* a SEALED (pqdm1:/pqdm2:) message never leaks a plaintext quoted preview as a
  cleartext sibling field (the seal payload is the body only, so quoted_* are
  suppressed on any sealed message rather than emitted alongside it).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.models import ChatMessage


class _StubBrain:
    """Offline stand-in for LuminaBrain.reply (no qwen HTTP)."""

    def reply(self, user_text, history=None, sender="chef"):
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(daemon_proxy, "_BRAIN", _StubBrain())
    monkeypatch.setattr(daemon_proxy, "_SEND_RECENT", {})
    monkeypatch.setattr(daemon_proxy, "_SEND_LOCKS", {})
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._hist = hist  # type: ignore[attr-defined]
    return c


def test_api_send_round_trips_quoted_fields_into_msg_to_app(client):
    """quoted_* sent to /v1/send survive persist+load and re-emit via _msg_to_app.

    Read back the operator's user turn from the Lumina thread (which loads the
    persisted JSONL and re-serializes through ``_msg_to_app``): the denormalized
    snippet must be present, mirroring the reply_to_id round-trip.
    """
    r = client.post(
        "/api/v1/send",
        json={
            "recipient": "lumina",
            "message": "sounds good",
            "reply_to_id": "orig-123",
            "quoted_text": "here is the original plan",
            "quoted_sender": "Lumina",
            "quoted_id": "orig-123",
        },
    )
    assert r.status_code == 200

    thread = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    user_turn = next(m for m in thread if m["content"] == "sounds good")
    assert user_turn["reply_to_id"] == "orig-123"
    assert user_turn["quoted_text"] == "here is the original plan"
    assert user_turn["quoted_sender"] == "Lumina"
    assert user_turn["quoted_id"] == "orig-123"


def test_persist_survives_jsonl_roundtrip(client):
    """_persist stores quoted_* and they survive a JSONL save + load cycle."""
    hist = client._hist
    daemon_proxy._persist(
        hist,
        daemon_proxy.OPERATOR_ID,
        "bob@skworld.io",
        "reply body",
        "orig-9",
        None,
        quoted_text="quoted preview",
        quoted_sender="You",
        quoted_id="orig-9",
    )
    loaded = hist.load(peer="bob@skworld.io", limit=10)
    assert loaded, "message not persisted"
    m = loaded[0]
    assert m.quoted_text == "quoted preview"
    assert m.quoted_sender == "You"
    assert m.quoted_id == "orig-9"


def test_plaintext_message_surfaces_quoted_fields():
    """A PLAINTEXT message surfaces quoted_* in the served app JSON."""
    m = ChatMessage(
        sender=daemon_proxy.LUMINA_URI,
        recipient=daemon_proxy.OPERATOR_ID,
        content="plaintext body",
        reply_to_id="orig-1",
        quoted_text="the quoted original",
        quoted_sender="You",
        quoted_id="orig-1",
    )
    out = daemon_proxy._msg_to_app(m, self_id=daemon_proxy.OPERATOR_ID)
    assert out["quoted_text"] == "the quoted original"
    assert out["quoted_sender"] == "You"
    assert out["quoted_id"] == "orig-1"


def test_sealed_message_does_not_leak_quoted_plaintext():
    """A SEALED (pqdm2:) body must NOT emit plaintext quoted_* siblings.

    Even if quoted_* somehow rode a sealed message, ``_msg_to_app`` suppresses
    them so the quoted preview never leaks in cleartext next to the ciphertext.
    """
    sealed_body = "pqdm2:" + "deadbeef" * 8
    m = ChatMessage(
        sender=daemon_proxy.LUMINA_URI,
        recipient=daemon_proxy.OPERATOR_ID,
        content=sealed_body,
        reply_to_id="orig-1",
        quoted_text="TOP SECRET quoted preview",
        quoted_sender="Lumina",
        quoted_id="orig-1",
    )
    out = daemon_proxy._msg_to_app(m, self_id=daemon_proxy.OPERATOR_ID)
    # Body is still the opaque token; the plaintext preview is NOT surfaced.
    assert out["body"] == sealed_body
    assert out["quoted_text"] is None
    assert out["quoted_sender"] is None
    assert out["quoted_id"] is None
    # And the leak string appears nowhere in the served payload.
    assert "TOP SECRET" not in str(out)


def test_group_msg_to_app_suppresses_quoted_on_sealed():
    """Group parity: a sealed group body suppresses quoted_* the same way."""
    sealed_body = "pqdm1:hybrid:" + "cafe" * 8
    m = ChatMessage(
        sender="alice@skworld.io",
        recipient="group:g1",
        content=sealed_body,
        quoted_text="secret group quote",
        quoted_sender="Alice",
        quoted_id="orig-7",
    )
    out = daemon_proxy._group_msg_to_app(m, group_id="g1")
    assert out["quoted_text"] is None
    assert "secret group quote" not in str(out)

    # Plaintext group message DOES surface them.
    m2 = ChatMessage(
        sender="alice@skworld.io",
        recipient="group:g1",
        content="plain group body",
        quoted_text="visible group quote",
        quoted_sender="Alice",
        quoted_id="orig-7",
    )
    out2 = daemon_proxy._group_msg_to_app(m2, group_id="g1")
    assert out2["quoted_text"] == "visible group quote"


# ── Sealed reply-quote envelope (skq1:) unwrap (fix/sealed-reply-quote) ────────


def test_unwrap_skq1_yields_body_and_quoted():
    """`skq1:` + json({t,q,qs,qi}) unwraps to the real body + quoted map."""
    import json

    payload = "skq1:" + json.dumps(
        {"t": "real body", "q": "the quote", "qs": "Chef", "qi": "orig-9"}
    )
    body, quoted = daemon_proxy._unwrap_skq1(payload)
    assert body == "real body"
    assert quoted == {
        "quoted_text": "the quote",
        "quoted_sender": "Chef",
        "quoted_id": "orig-9",
    }


def test_unwrap_skq1_plain_string_is_passthrough():
    """A plaintext without the prefix yields (string, {}) unchanged."""
    body, quoted = daemon_proxy._unwrap_skq1("just a normal message")
    assert body == "just a normal message"
    assert quoted == {}


def test_unwrap_skq1_malformed_falls_back_to_raw():
    """A `skq1:` prefix over non-JSON falls back to the raw string, empty map."""
    body, quoted = daemon_proxy._unwrap_skq1("skq1:{not valid json")
    assert body == "skq1:{not valid json"
    assert quoted == {}


def test_unwrap_skq1_wrapper_without_quote_is_body_only():
    """A wrapper carrying only `t` (no q/qs/qi) yields the body + empty map."""
    import json

    body, quoted = daemon_proxy._unwrap_skq1("skq1:" + json.dumps({"t": "hi"}))
    assert body == "hi"
    assert quoted == {}
