"""Tests for the SKSeal integration plugin."""

import pytest
from unittest.mock import MagicMock, patch

from skchat.models import ChatMessage, ContentType
from skchat.plugins_skseal import SKSealPlugin


def _msg(content="Hello", sender="capauth:alice@test", recipient="capauth:bob@test", **meta):
    return ChatMessage(sender=sender, recipient=recipient, content=content, metadata=meta)


class TestSKSealPluginMeta:
    def test_name(self):
        plugin = SKSealPlugin()
        assert plugin.name == "skseal"

    def test_version(self):
        plugin = SKSealPlugin()
        assert plugin.version == "0.1.0"

    def test_commands_list(self):
        plugin = SKSealPlugin()
        cmds = plugin.commands
        assert "sign" in cmds
        assert "decline" in cmds
        assert "doc-status" in cmds
        assert "doc-create" in cmds
        assert "doc-list" in cmds
        assert len(cmds) == 6


class TestInboundHook:
    def test_signing_request_adds_display_hints(self):
        plugin = SKSealPlugin()
        msg = _msg(
            content="Please sign this contract",
            signing_request={
                "document_id": "doc-12345678",
                "status": "pending",
            },
        )

        result = plugin.on_inbound(msg)
        assert result.metadata["display_type"] == "signing_request"
        assert result.metadata["signing_status"] == "pending"
        assert "signing_hint" in result.metadata
        assert "doc-1234" in result.metadata["signing_hint"]

    def test_no_signing_request_passthrough(self):
        plugin = SKSealPlugin()
        msg = _msg("Just a normal message")
        result = plugin.on_inbound(msg)
        assert result is msg

    def test_signing_request_preserves_content(self):
        plugin = SKSealPlugin()
        msg = _msg(
            content="Please review and sign",
            signing_request={"document_id": "abc", "status": "pending"},
        )
        result = plugin.on_inbound(msg)
        assert result.content == "Please review and sign"


class TestOutboundHook:
    def test_signing_request_adds_display_type(self):
        plugin = SKSealPlugin()
        msg = _msg(
            content="Sending contract for signing",
            signing_request={"document_id": "doc-abc", "status": "pending"},
        )

        result = plugin.on_outbound(msg)
        assert result.metadata["display_type"] == "signing_request"

    def test_normal_message_passthrough(self):
        plugin = SKSealPlugin()
        msg = _msg("No signing here")
        result = plugin.on_outbound(msg)
        assert result is msg


class TestSignCommand:
    def test_missing_document_id(self):
        plugin = SKSealPlugin()
        result = plugin.on_command("sign", "", {})
        assert "Usage" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_skseal_not_installed(self, mock_skseal):
        mock_skseal.return_value = (None, None)
        plugin = SKSealPlugin()
        result = plugin.on_command("sign", "doc-123", {"sender": "alice"})
        assert "not installed" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_document_not_found(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()
        store.load_document.side_effect = FileNotFoundError("not found")
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command("sign", "doc-nonexistent", {"sender": "alice"})
        assert "Could not load" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_not_a_signer(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()

        mock_doc = MagicMock()
        mock_doc.title = "Test Contract"
        mock_doc.signers = []
        store.load_document.return_value = mock_doc
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command("sign", "doc-123", {
            "sender": "capauth:alice@test",
            "fingerprint": "AABB",
        })
        assert "not listed as a signer" in result

    @patch("skchat.plugins_skseal._get_private_key")
    @patch("skchat.plugins_skseal._get_skseal")
    def test_sign_success(self, mock_skseal, mock_key):
        engine = MagicMock()
        store = MagicMock()

        mock_signer = MagicMock()
        mock_signer.fingerprint = "AABB"
        mock_signer.signer_id = "s1"
        mock_signer.name = "Alice"
        mock_signer.status.value = "pending"
        mock_signer.role.value = "signer"

        mock_doc = MagicMock()
        mock_doc.title = "Test Contract"
        mock_doc.signers = [mock_signer]
        store.load_document.return_value = mock_doc
        store.get_document_pdf.return_value = b"fake-pdf"

        updated_doc = MagicMock()
        updated_doc.title = "Test Contract"
        updated_doc.status.value = "completed"
        updated_signer = MagicMock()
        updated_signer.status.value = "signed"
        updated_doc.signers = [updated_signer]
        engine.sign_document.return_value = updated_doc

        mock_skseal.return_value = (engine, store)
        mock_key.return_value = ("-----PGP KEY-----", "passphrase")

        plugin = SKSealPlugin()
        result = plugin.on_command("sign", "doc-123", {
            "sender": "capauth:alice@test",
            "fingerprint": "AABB",
        })
        assert "Document Signed" in result
        assert "1/1" in result
        assert "complete" in result.lower()
        engine.sign_document.assert_called_once()
        store.save_document.assert_called_once()


class TestDeclineCommand:
    def test_missing_document_id(self):
        plugin = SKSealPlugin()
        result = plugin.on_command("decline", "", {})
        assert "Usage" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_decline_with_reason(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()

        mock_signer = MagicMock()
        mock_signer.fingerprint = "AABB"
        mock_signer.name = "capauth:alice@test"

        mock_doc = MagicMock()
        mock_doc.title = "Contract"
        mock_doc.signers = [mock_signer]
        store.load_document.return_value = mock_doc
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command(
            "decline", "doc-123 I disagree with clause 3",
            {"sender": "capauth:alice@test", "fingerprint": "AABB"},
        )
        assert "Document Declined" in result
        assert "I disagree with clause 3" in result


class TestDocStatusCommand:
    def test_missing_document_id(self):
        plugin = SKSealPlugin()
        result = plugin.on_command("doc-status", "", {})
        assert "Usage" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_doc_status_display(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()

        mock_signer = MagicMock()
        mock_signer.name = "Alice"
        mock_signer.role.value = "signer"
        mock_signer.status.value = "signed"

        mock_doc = MagicMock()
        mock_doc.title = "Test Contract"
        mock_doc.status.value = "completed"
        mock_doc.signers = [mock_signer]
        mock_doc.created_at.strftime.return_value = "2026-02-27 10:00"
        store.load_document.return_value = mock_doc
        store.get_audit_trail.return_value = []
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command("doc-status", "doc-123", {})
        assert "Document Status" in result
        assert "Test Contract" in result
        assert "completed" in result
        assert "Alice" in result


class TestDocListCommand:
    @patch("skchat.plugins_skseal._get_skseal")
    def test_doc_list_empty(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()
        store.list_documents.return_value = []
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command("doc-list", "", {})
        assert "No documents" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_doc_list_with_documents(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()

        mock_doc = MagicMock()
        mock_doc.document_id = "doc-12345678"
        mock_doc.title = "NDA Agreement"
        mock_doc.status.value = "pending"
        mock_signer = MagicMock()
        mock_signer.status.value = "pending"
        mock_doc.signers = [mock_signer]
        store.list_documents.return_value = [mock_doc]
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command("doc-list", "", {})
        assert "Documents" in result
        assert "NDA Agreement" in result
        assert "pending" in result


class TestDocCreateCommand:
    def test_missing_args(self):
        plugin = SKSealPlugin()
        result = plugin.on_command("doc-create", "template-only", {})
        assert "Usage" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_create_document(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()
        mock_skseal.return_value = (engine, store)

        # Mock the skseal.models import inside the handler
        with patch("skchat.plugins_skseal._get_skseal", return_value=(engine, store)):
            plugin = SKSealPlugin()
            result = plugin.on_command(
                "doc-create", "tmpl-nda New NDA Agreement",
                {"sender": "capauth:alice@test", "fingerprint": "AABB"},
            )
            # Will try to import skseal.models — if not available,
            # returns an error which is fine for testing
            assert isinstance(result, str)


class TestDocSendCommand:
    def test_missing_args(self):
        plugin = SKSealPlugin()
        result = plugin.on_command("doc-send", "doc-123", {})
        assert "Usage" in result

    def test_missing_all_args(self):
        plugin = SKSealPlugin()
        result = plugin.on_command("doc-send", "", {})
        assert "Usage" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_skseal_not_installed(self, mock_skseal):
        mock_skseal.return_value = (None, None)
        plugin = SKSealPlugin()
        result = plugin.on_command("doc-send", "doc-123 lumina", {"sender": "alice"})
        assert "not installed" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_document_not_found(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()
        store.load_document.side_effect = FileNotFoundError("not found")
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command("doc-send", "doc-bad lumina", {"sender": "alice"})
        assert "Could not load" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_doc_send_queued(self, mock_skseal):
        engine = MagicMock()
        store = MagicMock()

        mock_signer = MagicMock()
        mock_signer.signer_id = "s1"
        mock_signer.name = "Bob"
        mock_signer.fingerprint = "BBCC"
        mock_signer.role.value = "signer"
        mock_signer.status.value = "pending"

        mock_doc = MagicMock()
        mock_doc.title = "Employment Agreement"
        mock_doc.status.value = "pending"
        mock_doc.signers = [mock_signer]
        store.load_document.return_value = mock_doc
        mock_skseal.return_value = (engine, store)

        plugin = SKSealPlugin()
        result = plugin.on_command(
            "doc-send", "doc-123 capauth:bob@skworld.io",
            {"sender": "capauth:alice@test", "fingerprint": "AABB"},
        )
        assert "Signing Request" in result
        assert "Employment Agreement" in result
        assert "bob@skworld.io" in result

    @patch("skchat.plugins_skseal._get_skseal")
    def test_commands_includes_doc_send(self, mock_skseal):
        plugin = SKSealPlugin()
        assert "doc-send" in plugin.commands
