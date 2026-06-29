"""Tests for the AI-native consent MCP tools in ``skchat.mcp_server``.

These three tools are the surface that lets Lumina/Opus (or the Telegram bot)
say "X wants to connect — accept?" and act on the answer. They are a thin MCP
facade over the already-built consent backend in ``skcomms`` (``consent_requests``
+ ``consent_pipeline``):

* ``list_contact_requests``   — the pending first-contact knock queue.
* ``accept_contact_request``  — promote sender to known + mint a delivery token.
* ``decline_contact_request`` — drop the knock, optionally block the sender.

The recipient agent is the node's own ``SKAGENT`` identity. We drive the tools by
invoking ``call_tool(...)`` directly against a RequestQueue seeded under a temp
``SKCOMMS_HOME`` — exactly the inbound-gate's source of truth.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from skchat import mcp_server


def _payload(result) -> dict | list:
    """Extract the JSON payload from a list[TextContent] tool result."""
    assert result, "tool returned an empty result"
    return json.loads(result[0].text)


@pytest.fixture()
def consent_env(tmp_path, monkeypatch):
    """Point skcomms at a temp home and pin the running agent to 'testagent'."""
    monkeypatch.setenv("SKCOMMS_HOME", str(tmp_path / "skcomms"))
    monkeypatch.setenv("SKAGENT", "testagent")
    monkeypatch.delenv("SKCHAT_IDENTITY", raising=False)
    return "testagent"


def _seed_request(agent: str, sender: str, *, envelope_id: str = "env-1") -> None:
    """Enqueue a quarantined first-contact knock for *agent*."""
    from skcomms.consent import RequestQueue

    RequestQueue(agent).enqueue(sender, b"hello", envelope_id=envelope_id)


def test_list_contact_requests_empty(consent_env):
    result = asyncio.run(mcp_server.call_tool("list_contact_requests", {}))
    data = _payload(result)
    assert data["agent"] == "testagent"
    assert data["count"] == 0
    assert data["requests"] == []


def test_list_contact_requests_surfaces_pending(consent_env):
    _seed_request("testagent", "alice@home.skworld", envelope_id="env-42")
    result = asyncio.run(mcp_server.call_tool("list_contact_requests", {}))
    data = _payload(result)
    assert data["count"] == 1
    req = data["requests"][0]
    assert req["sender"] == "alice@home.skworld"
    assert req["envelope_id"] == "env-42"
    assert "received_at" in req


def test_accept_contact_request_promotes_and_mints_token(consent_env):
    _seed_request("testagent", "bob@home.skworld")
    result = asyncio.run(
        mcp_server.call_tool("accept_contact_request", {"sender": "bob@home.skworld"})
    )
    data = _payload(result)
    assert data["result"] == "accepted"
    assert data["sender"] == "bob@home.skworld"
    # on_accept mints a per-contact delivery token.
    assert data.get("token")
    assert isinstance(data["token"], str)

    # Queue is cleared and sender is now a known contact.
    from skcomms import consent_requests

    assert consent_requests.list_requests("testagent") == []
    assert "bob@home.skworld" in consent_requests.list_known("testagent")


def test_accept_requires_sender(consent_env):
    result = asyncio.run(mcp_server.call_tool("accept_contact_request", {}))
    data = _payload(result)
    assert "error" in data


def test_decline_contact_request_clears_queue(consent_env):
    _seed_request("testagent", "carol@home.skworld")
    result = asyncio.run(
        mcp_server.call_tool("decline_contact_request", {"sender": "carol@home.skworld"})
    )
    data = _payload(result)
    assert data["result"] == "declined"
    assert data["sender"] == "carol@home.skworld"

    from skcomms import consent_requests

    assert consent_requests.list_requests("testagent") == []
    # Declined (not blocked) → not promoted to known.
    assert "carol@home.skworld" not in consent_requests.list_known("testagent")


def test_decline_contact_request_with_block(consent_env):
    _seed_request("testagent", "dave@home.skworld")
    result = asyncio.run(
        mcp_server.call_tool(
            "decline_contact_request",
            {"sender": "dave@home.skworld", "block": True},
        )
    )
    data = _payload(result)
    assert data["result"] == "blocked"

    from skcomms.consent import ContactStore

    assert ContactStore("testagent").is_blocked("dave@home.skworld")


def test_decline_requires_sender(consent_env):
    result = asyncio.run(mcp_server.call_tool("decline_contact_request", {}))
    data = _payload(result)
    assert "error" in data


def test_consent_tools_registered_in_list_tools():
    tools = asyncio.run(mcp_server.list_tools())
    names = {t.name for t in tools}
    assert {
        "list_contact_requests",
        "accept_contact_request",
        "decline_contact_request",
    } <= names
