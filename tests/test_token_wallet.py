"""Tests for the SKChat gate-4 sender-side TokenWallet + transport attach.

The wallet is the *sender's* stash of per-contact delivery tokens RECEIVED from
recipients who accepted them (skcomms gate-4, ``ConsentPipeline.on_accept`` mints
the token; the recipient mails it back in an ACCEPT/contact-grant message). When
this agent later DMs that contact, the token rides along in the message metadata
(``consent_token``) so the recipient's gate-4 verifies + fast-paths DELIVER.

State is isolated per test via SKCHAT_HOME so a fresh wallet sees exactly what
the test stored, and per-agent isolation is verified directly.
"""

from __future__ import annotations

import json

import pytest

from skchat.models import ChatMessage
from skchat.token_wallet import (
    CONSENT_ACCEPT_KEY,
    CONSENT_TOKEN_KEY,
    TokenWallet,
    build_accept_message,
    extract_accept_token,
)

AGENT = "testbot"
PEER = "alice@operator.realm"
TOKEN = "deadbeef" * 8  # 64 hex chars, the shape consent_tokens issues


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat"))
    yield


# ── wallet core ────────────────────────────────────────────────────────────


def test_store_and_get_roundtrip():
    w = TokenWallet(AGENT)
    w.store_token(PEER, TOKEN)
    assert w.get_token(PEER) == TOKEN


def test_get_missing_returns_none():
    w = TokenWallet(AGENT)
    assert w.get_token("nobody@nowhere") is None


def test_drop_removes_token():
    w = TokenWallet(AGENT)
    w.store_token(PEER, TOKEN)
    w.drop(PEER)
    assert w.get_token(PEER) is None


def test_drop_missing_is_noop():
    w = TokenWallet(AGENT)
    w.drop(PEER)  # must not raise
    assert w.get_token(PEER) is None


def test_persists_across_instances():
    TokenWallet(AGENT).store_token(PEER, TOKEN)
    # A brand-new wallet over the same SKCHAT_HOME re-reads the persisted token.
    assert TokenWallet(AGENT).get_token(PEER) == TOKEN


def test_per_agent_isolation():
    TokenWallet("agent_a").store_token(PEER, TOKEN)
    # A different agent's wallet must not see agent_a's token.
    assert TokenWallet("agent_b").get_token(PEER) is None


def test_capauth_prefix_normalised():
    # Stored under a capauth: URI, fetched by bare fqid (and vice-versa).
    w = TokenWallet(AGENT)
    w.store_token(f"capauth:{PEER}", TOKEN)
    assert w.get_token(PEER) == TOKEN
    assert w.get_token(f"capauth:{PEER}") == TOKEN


# ── ACCEPT message shape ────────────────────────────────────────────────────


def test_build_and_extract_accept_roundtrip():
    # The recipient (grantor) mints a token and mails an ACCEPT back to the requester.
    msg = build_accept_message(sender=PEER, recipient=f"capauth:{AGENT}@skworld.io", token=TOKEN)
    assert msg.metadata.get(CONSENT_ACCEPT_KEY) is True
    assert msg.metadata.get(CONSENT_TOKEN_KEY) == TOKEN
    grantor, token = extract_accept_token(msg)
    assert grantor == PEER
    assert token == TOKEN


def test_extract_non_accept_returns_none():
    plain = ChatMessage(sender=PEER, recipient="capauth:x@y", content="hi there")
    assert extract_accept_token(plain) is None


# ── transport wiring (gated by SKCOMMS_CONSENT_MODE) ────────────────────────


def _make_transport(monkeypatch):
    from unittest.mock import MagicMock

    from skchat.transport import ChatTransport

    skcomms = MagicMock()
    skcomms.send.return_value = MagicMock(delivered=True, successful_transport="syncthing")
    skcomms.receive.return_value = []
    # No federation path — plain local send.
    if hasattr(skcomms, "send_federated"):
        del skcomms.send_federated
    history = MagicMock()
    t = ChatTransport(
        skcomms=skcomms,
        history=history,
        identity=f"capauth:{AGENT}@skworld.io",
    )
    return t, skcomms


def test_send_attaches_stored_token(monkeypatch):
    monkeypatch.setenv("SKCOMMS_CONSENT_MODE", "public")
    TokenWallet(AGENT).store_token(PEER, TOKEN)
    t, skcomms = _make_transport(monkeypatch)

    msg = ChatMessage(sender=f"capauth:{AGENT}@skworld.io", recipient=PEER, content="yo")
    t.send_message(msg)

    sent_payload = skcomms.send.call_args.kwargs["message"]
    body = json.loads(sent_payload)
    assert body["metadata"].get(CONSENT_TOKEN_KEY) == TOKEN


def test_send_without_token_attaches_nothing(monkeypatch):
    monkeypatch.setenv("SKCOMMS_CONSENT_MODE", "public")
    t, skcomms = _make_transport(monkeypatch)  # empty wallet

    msg = ChatMessage(sender=f"capauth:{AGENT}@skworld.io", recipient=PEER, content="yo")
    t.send_message(msg)

    body = json.loads(skcomms.send.call_args.kwargs["message"])
    assert CONSENT_TOKEN_KEY not in body.get("metadata", {})


def test_send_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("SKCOMMS_CONSENT_MODE", raising=False)
    TokenWallet(AGENT).store_token(PEER, TOKEN)
    t, skcomms = _make_transport(monkeypatch)

    msg = ChatMessage(sender=f"capauth:{AGENT}@skworld.io", recipient=PEER, content="yo")
    t.send_message(msg)

    body = json.loads(skcomms.send.call_args.kwargs["message"])
    # Consent OFF (no SKCOMMS_CONSENT_MODE) → no token attached even if stored.
    assert CONSENT_TOKEN_KEY not in body.get("metadata", {})


def test_inbound_accept_stores_token(monkeypatch):
    monkeypatch.setenv("SKCOMMS_CONSENT_MODE", "public")
    t, _ = _make_transport(monkeypatch)

    accept = build_accept_message(
        sender=PEER, recipient=f"capauth:{AGENT}@skworld.io", token=TOKEN
    )
    t._maybe_store_consent_token(accept)

    assert TokenWallet(AGENT).get_token(PEER) == TOKEN


def test_inbound_accept_ignored_when_gated_off(monkeypatch):
    monkeypatch.delenv("SKCOMMS_CONSENT_MODE", raising=False)
    t, _ = _make_transport(monkeypatch)

    accept = build_accept_message(
        sender=PEER, recipient=f"capauth:{AGENT}@skworld.io", token=TOKEN
    )
    t._maybe_store_consent_token(accept)

    assert TokenWallet(AGENT).get_token(PEER) is None
