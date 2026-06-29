"""Gate-4 sender side: lift the wallet token onto the skcomms ENVELOPE.

The recipient's gate-4 reads ``Envelope.consent_token`` (outside the ratchet-sealed
body), so the sender must lift its held per-contact token from the
:class:`skchat.token_wallet.TokenWallet` onto the skcomms federation envelope — NOT
only into the inner ChatMessage metadata (which is opaque once the body is sealed).

These tests pin that ``send_message``:

* forwards ``consent_token=<held token>`` to ``skcomms.send_federated`` when consent
  is ON and we hold a token for the recipient,
* is inert (no ``consent_token`` kwarg) when consent is OFF or no token is held,
* degrades gracefully when the underlying ``send_federated`` predates the kwarg
  (forward/back-compatible — never raises, still delivers).

Run from HOME (skmemory namespace collision): ``cd ~ && ~/.skenv/bin/python -m pytest``.
"""

from __future__ import annotations

import pytest

from skchat.history import ChatHistory
from skchat.models import ChatMessage
from skchat.token_wallet import TokenWallet
from skchat.transport import ChatTransport


class FakeReport:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.successful_transport = "https-s2s" if delivered else None


class FakeSkcommsKwargs:
    """send_federated accepts **kw (like a consent-aware build)."""

    def __init__(self):
        self.federated_calls = []
        self.legacy_calls = []

    def send_federated(self, to_fqid, message, **kw):
        self.federated_calls.append((to_fqid, message, kw))
        return FakeReport(True)

    def send(self, recipient, message, **kw):
        self.legacy_calls.append((recipient, message, kw))
        return FakeReport(True)


class FakeSkcommsLegacyFed:
    """send_federated WITHOUT consent_token support (no **kwargs) — must not crash."""

    def __init__(self):
        self.federated_calls = []

    def send_federated(self, to_fqid, message, *, thread_id=None, in_reply_to=None):
        self.federated_calls.append((to_fqid, message, thread_id, in_reply_to))
        return FakeReport(True)

    def send(self, recipient, message, **kw):  # pragma: no cover - fallback only
        return FakeReport(True)


RECIPIENT = "lumina@chef.skworld"
TOKEN = "a1b2c3" * 10  # 60 hex chars


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    # Make the DM ratchet path inert so we exercise the classical/consent path only.
    monkeypatch.delenv("SKCHAT_DM_RATCHET", raising=False)
    return tmp_path


def _tx(sk, home):
    return ChatTransport(
        skcomms=sk,
        history=ChatHistory(history_dir=home / "h"),
        identity="capauth:jarvis@skworld.io",
    )


def _msg(recipient=RECIPIENT):
    return ChatMessage(sender="capauth:jarvis@skworld.io", recipient=recipient, content="hi")


def test_token_lifted_onto_envelope_when_consent_on(home, monkeypatch):
    monkeypatch.setenv("SKCOMMS_CONSENT_MODE", "public")
    # jarvis holds a delivery token granted by lumina.
    TokenWallet("jarvis").store_token(RECIPIENT, TOKEN)

    sk = FakeSkcommsKwargs()
    tx = _tx(sk, home)
    monkeypatch.setattr(tx, "_federation_target", lambda r: RECIPIENT)

    res = tx.send_message(_msg())
    assert res["delivered"] is True
    assert sk.federated_calls, "should have routed via federation"
    _to, _payload, kw = sk.federated_calls[0]
    assert kw.get("consent_token") == TOKEN


def test_no_token_kwarg_when_consent_off(home, monkeypatch):
    monkeypatch.delenv("SKCOMMS_CONSENT_MODE", raising=False)
    TokenWallet("jarvis").store_token(RECIPIENT, TOKEN)  # token present but consent OFF

    sk = FakeSkcommsKwargs()
    tx = _tx(sk, home)
    monkeypatch.setattr(tx, "_federation_target", lambda r: RECIPIENT)

    tx.send_message(_msg())
    assert sk.federated_calls
    _to, _payload, kw = sk.federated_calls[0]
    assert "consent_token" not in kw


def test_no_token_kwarg_when_wallet_empty(home, monkeypatch):
    monkeypatch.setenv("SKCOMMS_CONSENT_MODE", "public")
    # No token stored for this recipient.

    sk = FakeSkcommsKwargs()
    tx = _tx(sk, home)
    monkeypatch.setattr(tx, "_federation_target", lambda r: RECIPIENT)

    tx.send_message(_msg())
    assert sk.federated_calls
    _to, _payload, kw = sk.federated_calls[0]
    assert "consent_token" not in kw


def test_graceful_when_send_federated_lacks_kwarg(home, monkeypatch):
    """A send_federated that predates consent_token must not crash and still delivers."""
    monkeypatch.setenv("SKCOMMS_CONSENT_MODE", "public")
    TokenWallet("jarvis").store_token(RECIPIENT, TOKEN)

    sk = FakeSkcommsLegacyFed()
    tx = _tx(sk, home)
    monkeypatch.setattr(tx, "_federation_target", lambda r: RECIPIENT)

    res = tx.send_message(_msg())
    assert res["delivered"] is True
    assert sk.federated_calls  # delivered without the kwarg
