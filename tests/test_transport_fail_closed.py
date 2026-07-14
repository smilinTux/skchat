"""P0.1b/P0.2 — fail-closed ratchet send + signing/confidentiality decoupling.

Covers ``skchat.transport.ChatTransport``:

* A peer with a **live ratchet session** that cannot seal MUST raise
  :class:`~skchat.transport.ConfidentialityError` instead of silently handing a
  classical plaintext envelope to skcomms (HNDL downgrade).
* A missing PGP *signing* key MUST NOT disable the ratchet/confidentiality; it
  is surfaced as :attr:`ChatTransport.signing_degraded` while the ratchet keeps
  working with a hybrid key.
* The classical / no-ratchet path is byte-for-byte unchanged (plaintext still
  goes out).

The DM ratchet manager is injected via ``_dm_mgr_cached`` so these are pure unit
tests — no filesystem, no PQ backend.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skchat.models import ChatMessage
from skchat.transport import ChatTransport, ConfidentialityError


@pytest.fixture
def mock_skcomms():
    comm = MagicMock()
    comm.send.return_value = MagicMock(delivered=True, successful_transport="file")
    comm.receive.return_value = []
    return comm


@pytest.fixture
def mock_history():
    history = MagicMock()
    history.store_message.return_value = "mem-1"
    return history


def _transport(skcomms, history, crypto=None):
    # A non-local, non-federated recipient keeps the send on the plain skcomms
    # path (no loopback / federation branch) unless the ratchet intervenes.
    return ChatTransport(
        skcomms=skcomms,
        history=history,
        identity="capauth:alice@skworld.io",
        crypto=crypto,
    )


def _msg():
    return ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content="top secret",
    )


class TestFailClosedRatchet:
    def test_live_ratchet_seal_raising_is_fail_closed(self, mock_skcomms, mock_history):
        """A live-ratchet peer whose seal RAISES → ConfidentialityError, no plaintext."""
        crypto = MagicMock()
        transport = _transport(mock_skcomms, mock_history, crypto=crypto)

        mgr = MagicMock()
        mgr.can_ratchet.return_value = True
        mgr.seal.side_effect = RuntimeError("PQ backend exploded")
        transport._dm_mgr_cached = mgr  # inject a live ratchet manager

        with pytest.raises(ConfidentialityError):
            transport.send_message(_msg())

        # HARD requirement: no plaintext envelope was ever handed to skcomms.
        mock_skcomms.send.assert_not_called()

    def test_live_ratchet_seal_returning_unsealed_is_fail_closed(
        self, mock_skcomms, mock_history
    ):
        """Seal returns the body UNSEALED (no ratchet frame) → ConfidentialityError."""
        crypto = MagicMock()
        crypto.is_ratchet_message.return_value = False  # seal did not produce a frame
        transport = _transport(mock_skcomms, mock_history, crypto=crypto)

        mgr = MagicMock()
        mgr.can_ratchet.return_value = True
        mgr.seal.side_effect = lambda m: m  # untouched — classical fallback would leak
        transport._dm_mgr_cached = mgr

        with pytest.raises(ConfidentialityError):
            transport.send_message(_msg())

        mock_skcomms.send.assert_not_called()

    def test_live_ratchet_seal_success_sends_sealed_body(self, mock_skcomms, mock_history):
        """A successful seal is delivered; the plaintext never reaches skcomms."""
        crypto = MagicMock()
        crypto.is_ratchet_message.return_value = True
        transport = _transport(mock_skcomms, mock_history, crypto=crypto)

        msg = _msg()
        sealed = msg.model_copy(update={"content": "pqdr1:sealed-body", "encrypted": True})
        mgr = MagicMock()
        mgr.can_ratchet.return_value = True
        mgr.seal.return_value = sealed
        transport._dm_mgr_cached = mgr

        result = transport.send_message(msg)

        assert result["delivered"] is True
        mock_skcomms.send.assert_called_once()
        sent_message = mock_skcomms.send.call_args.kwargs["message"]
        assert "top secret" not in sent_message
        assert "pqdr1:sealed-body" in sent_message


class TestClassicalUnchanged:
    def test_no_ratchet_still_sends_plaintext(self, mock_skcomms, mock_history):
        """mgr is None (ratchet disabled) → the classical plaintext path is intact."""
        crypto = MagicMock()
        transport = _transport(mock_skcomms, mock_history, crypto=crypto)
        transport._dm_mgr_cached = None  # ratchet disabled

        result = transport.send_message(_msg())

        assert result["delivered"] is True
        mock_skcomms.send.assert_called_once()
        assert "top secret" in mock_skcomms.send.call_args.kwargs["message"]

    def test_no_crypto_still_sends_plaintext(self, mock_skcomms, mock_history):
        """No crypto at all → classical baseline, plaintext, not 'degraded'."""
        transport = _transport(mock_skcomms, mock_history, crypto=None)

        result = transport.send_message(_msg())

        assert result["delivered"] is True
        assert transport.signing_degraded is False
        mock_skcomms.send.assert_called_once()


class TestSigningDegraded:
    def test_signing_degraded_false_with_signing_crypto(self, mock_skcomms, mock_history):
        crypto = MagicMock()
        crypto.can_sign = True
        transport = _transport(mock_skcomms, mock_history, crypto=crypto)
        assert transport.signing_degraded is False

    def test_signing_degraded_false_without_crypto(self, mock_skcomms, mock_history):
        transport = _transport(mock_skcomms, mock_history, crypto=None)
        assert transport.signing_degraded is False

    def test_signing_degraded_false_legacy_crypto_without_attr(
        self, mock_skcomms, mock_history
    ):
        """A crypto object predating ``can_sign`` is assumed to sign (no false alarm)."""

        class _LegacyCrypto:
            pass

        transport = _transport(mock_skcomms, mock_history, crypto=_LegacyCrypto())
        assert transport.signing_degraded is False

    def test_missing_signing_key_sets_degraded_but_ratchet_still_works(
        self, mock_skcomms, mock_history
    ):
        """Missing PGP signing key → signing_degraded True, ratchet still seals."""
        crypto = MagicMock()
        crypto.can_sign = False  # ratchet-only ChatCrypto (no PGP signing key)
        crypto.is_ratchet_message.return_value = True
        transport = _transport(mock_skcomms, mock_history, crypto=crypto)

        assert transport.signing_degraded is True

        msg = _msg()
        sealed = msg.model_copy(update={"content": "pqdr1:x", "encrypted": True})
        mgr = MagicMock()
        mgr.can_ratchet.return_value = True
        mgr.seal.return_value = sealed
        transport._dm_mgr_cached = mgr

        result = transport.send_message(msg)

        assert result["delivered"] is True
        mgr.seal.assert_called_once()
        assert "top secret" not in mock_skcomms.send.call_args.kwargs["message"]
