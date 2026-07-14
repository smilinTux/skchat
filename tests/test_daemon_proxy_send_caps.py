"""Tests for the P0.6 versioned-contract + dual-serve scaffolding on
``POST /api/v1/send`` in ``skchat.daemon_proxy``.

Contract:
  * A client announces the contract revisions / features it understands via a
    single request header (``X-SKChat-Client-Caps``). The handler parses it and
    can gate behavior on it (dual-serve seam for a future contract change /
    P0.5 auth enforcement).
  * A client that sends NO header keeps the current (legacy) behavior verbatim —
    every capability gate defaults to "off" when the header is absent.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy


class _StubBrain:
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


# --------------------------------------------------------------------------- #
# Negotiation helpers (unit level)
# --------------------------------------------------------------------------- #
def test_parse_client_caps_empty_when_no_header():
    assert daemon_proxy._parse_client_caps(None) == {}
    assert daemon_proxy._parse_client_caps("") == {}
    assert daemon_proxy._parse_client_caps("   ") == {}


def test_parse_client_caps_flags_and_key_values():
    caps = daemon_proxy._parse_client_caps("v=2, typed-envelope , Auth")
    # bare flags -> True, key=value -> value, keys are case-insensitive.
    assert caps["v"] == "2"
    assert caps["typed-envelope"] is True
    assert caps["auth"] is True


def test_client_supports_gate_defaults_off():
    # Absent capability / empty caps -> legacy path (False).
    assert daemon_proxy._client_supports({}, "auth") is False
    caps = daemon_proxy._parse_client_caps("typed-envelope")
    assert daemon_proxy._client_supports(caps, "typed-envelope") is True
    assert daemon_proxy._client_supports(caps, "auth") is False


# --------------------------------------------------------------------------- #
# Negotiation over the real HTTP surface
# --------------------------------------------------------------------------- #
def test_send_without_caps_header_keeps_default_behavior(client):
    """No header -> the response carries NO negotiated-contract additions; the
    legacy send contract is byte-for-byte what deployed clients already get."""
    r = client.post("/api/v1/send", json={"recipient": "bob", "message": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["recipient"] == "bob"
    # The dual-serve addition is absent for a client that announced nothing.
    assert "client_caps" not in body


def test_send_with_caps_header_is_parsed_and_exposed(client):
    """A capable client's negotiated caps are parsed and exposed back through the
    handler (the dual-serve branch), without affecting legacy clients."""
    r = client.post(
        "/api/v1/send",
        json={"recipient": "bob", "message": "hi"},
        headers={daemon_proxy.CLIENT_CAPS_HEADER: "v=2, typed-envelope"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["client_caps"]["v"] == "2"
    assert body["client_caps"]["typed-envelope"] is True


def test_lumina_send_with_caps_header_still_replies(client):
    """The Lumina brain path also honors the seam: capable clients get the reply
    AND the negotiated caps; legacy behavior (the reply) is unchanged."""
    r = client.post(
        "/api/v1/send",
        json={"recipient": "lumina", "message": "hi there"},
        headers={daemon_proxy.CLIENT_CAPS_HEADER: "v=2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reply"]["content"] == "Lumina hears you: hi there"
    assert body["client_caps"]["v"] == "2"
