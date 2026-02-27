"""SKSeal integration plugin — in-chat document signing.

Receive, review, and sign documents directly in SKChat conversations.
When a signing request arrives, it appears as a special message with
document details. Recipients can sign with /sign or decline with /decline.

Commands:
    /sign <document_id>                Sign a document with your PGP key
    /decline <document_id> [reason]    Decline to sign a document
    /doc-status <document_id>          Check signing status and audit trail
    /doc-create <template_id> <title>  Create a new signing request
    /doc-list [--status STATUS]        List documents

This plugin requires skseal to be installed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .models import ChatMessage
from .plugins import ChatPlugin

logger = logging.getLogger("skchat.plugins.skseal")


def _get_skseal():
    """Try to import and initialize SKSeal components.

    Returns:
        tuple: (SealEngine, DocumentStore) or (None, None) if unavailable.
    """
    try:
        from skseal.engine import SealEngine
        from skseal.store import DocumentStore

        return SealEngine(), DocumentStore()
    except ImportError:
        return None, None


def _get_private_key():
    """Load the local user's PGP private key for signing.

    Returns:
        tuple: (private_key_armor, passphrase) or (None, None).
    """
    from pathlib import Path
    import json

    identity_dir = Path.home() / ".skcapstone" / "identity"

    # Check for key path in identity config
    identity_file = identity_dir / "identity.json"
    if identity_file.exists():
        try:
            with open(identity_file) as f:
                data = json.load(f)
            key_path = data.get("private_key_path")
            if key_path:
                key_path = Path(key_path).expanduser()
                if key_path.exists():
                    armor = key_path.read_text()
                    passphrase = data.get("passphrase", "")
                    return armor, passphrase
        except Exception:
            pass

    # Check GPG keyring via gpg export
    try:
        import subprocess

        result = subprocess.run(
            ["gpg", "--export-secret-keys", "--armor"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and "PGP PRIVATE KEY" in result.stdout:
            return result.stdout, ""
    except Exception:
        pass

    return None, None


class SKSealPlugin(ChatPlugin):
    """In-chat document signing via SKSeal.

    Provides slash commands for creating, signing, declining, and
    checking status of documents. Signing requests in message metadata
    are rendered as rich message bubbles.

    Commands: sign, decline, doc-status, doc-create, doc-list
    """

    name = "skseal"
    version = "0.1.0"
    description = "In-chat document signing via SKSeal"
    author = "smilinTux"

    @property
    def commands(self) -> list[str]:
        return ["sign", "decline", "doc-status", "doc-create", "doc-list"]

    def activate(self) -> None:
        """Verify SKSeal is available on activation."""
        try:
            import skseal  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "skseal not installed. Run: pip install skseal"
            )

    def on_inbound(self, message: ChatMessage) -> ChatMessage:
        """Detect signing requests in inbound messages.

        If a message contains a signing_request in metadata, add
        display hints for the UI layer.
        """
        signing_request = message.metadata.get("signing_request")
        if signing_request:
            metadata = dict(message.metadata)
            metadata["display_type"] = "signing_request"
            metadata["signing_status"] = signing_request.get("status", "pending")
            doc_id = signing_request.get("document_id", "unknown")
            metadata["signing_hint"] = (
                f"Signing request for document {doc_id[:8]}. "
                f"Use /sign {doc_id} to sign or /decline {doc_id} to decline."
            )
            return message.model_copy(update={"metadata": metadata})
        return message

    def on_outbound(self, message: ChatMessage) -> ChatMessage:
        """Attach signing metadata to outbound signing requests."""
        signing_request = message.metadata.get("signing_request")
        if signing_request:
            metadata = dict(message.metadata)
            metadata["display_type"] = "signing_request"
            return message.model_copy(update={"metadata": metadata})
        return message

    def on_command(self, command: str, args: str, context: dict) -> Optional[str]:
        if command == "sign":
            return self._handle_sign(args, context)
        elif command == "decline":
            return self._handle_decline(args, context)
        elif command == "doc-status":
            return self._handle_doc_status(args, context)
        elif command == "doc-create":
            return self._handle_doc_create(args, context)
        elif command == "doc-list":
            return self._handle_doc_list(args, context)
        return None

    def _handle_sign(self, args: str, context: dict) -> str:
        """Handle /sign <document_id> command."""
        document_id = args.strip()
        if not document_id:
            return "Usage: /sign <document_id>"

        engine, store = _get_skseal()
        if engine is None:
            return "Error: skseal not installed."

        try:
            document = store.load_document(document_id)
        except Exception as exc:
            return f"Error: Could not load document '{document_id[:12]}': {exc}"

        sender = context.get("sender", "unknown")
        fingerprint = context.get("fingerprint", "")

        # Find the signer record for this sender
        signer = None
        for s in document.signers:
            if s.fingerprint == fingerprint or s.name == sender:
                signer = s
                break

        if signer is None:
            return (
                f"Error: You ({sender}) are not listed as a signer "
                f"on document '{document.title}'."
            )

        if signer.status.value == "signed":
            return f"You already signed '{document.title}'."

        # Load private key
        private_key_armor, passphrase = _get_private_key()
        if private_key_armor is None:
            return (
                "Error: No PGP private key found. "
                "Configure your key in ~/.skcapstone/identity/identity.json"
            )

        # Get the PDF data
        pdf_data = store.get_document_pdf(document_id)

        try:
            updated_doc = engine.sign_document(
                document=document,
                signer_id=signer.signer_id,
                private_key_armor=private_key_armor,
                passphrase=passphrase,
                pdf_data=pdf_data,
            )
            store.save_document(updated_doc)

            status = updated_doc.status.value
            signed_count = sum(
                1 for s in updated_doc.signers if s.status.value == "signed"
            )
            total = len(updated_doc.signers)

            result = (
                f"**Document Signed**\n"
                f"Title: {updated_doc.title}\n"
                f"ID: {document_id[:12]}\n"
                f"Progress: {signed_count}/{total} signatures\n"
                f"Status: {status}"
            )

            if status == "completed":
                result += "\n\nAll signatures collected. Document is complete."

            return result

        except Exception as exc:
            return f"Error signing document: {exc}"

    def _handle_decline(self, args: str, context: dict) -> str:
        """Handle /decline <document_id> [reason] command."""
        parts = args.strip().split(None, 1)
        if not parts:
            return "Usage: /decline <document_id> [reason]"

        document_id = parts[0]
        reason = parts[1] if len(parts) > 1 else "No reason provided"

        engine, store = _get_skseal()
        if engine is None:
            return "Error: skseal not installed."

        try:
            document = store.load_document(document_id)
        except Exception as exc:
            return f"Error: Could not load document '{document_id[:12]}': {exc}"

        sender = context.get("sender", "unknown")
        fingerprint = context.get("fingerprint", "")

        for s in document.signers:
            if s.fingerprint == fingerprint or s.name == sender:
                s.status = "declined"
                break
        else:
            return f"Error: You are not a signer on '{document.title}'."

        # Add audit entry
        try:
            from skseal.models import AuditEntry, AuditAction

            entry = AuditEntry(
                document_id=document_id,
                action=AuditAction.DECLINED,
                actor_fingerprint=fingerprint or sender,
                details=f"Declined: {reason}",
            )
            store.append_audit(entry)
            store.save_document(document)
        except Exception:
            pass

        return (
            f"**Document Declined**\n"
            f"Title: {document.title}\n"
            f"Reason: {reason}"
        )

    def _handle_doc_status(self, args: str, context: dict) -> str:
        """Handle /doc-status <document_id> command."""
        document_id = args.strip()
        if not document_id:
            return "Usage: /doc-status <document_id>"

        engine, store = _get_skseal()
        if engine is None:
            return "Error: skseal not installed."

        try:
            document = store.load_document(document_id)
        except Exception as exc:
            return f"Error: Could not load document '{document_id[:12]}': {exc}"

        signers_info = []
        for s in document.signers:
            status_icon = {
                "signed": "+",
                "pending": "?",
                "declined": "x",
                "viewed": "~",
                "expired": "!",
            }.get(s.status.value, "?")
            signers_info.append(
                f"  [{status_icon}] {s.name} ({s.role.value}) — {s.status.value}"
            )

        signers_str = "\n".join(signers_info)

        audit = store.get_audit_trail(document_id)
        recent_audit = audit[-5:] if len(audit) > 5 else audit
        audit_lines = []
        for entry in recent_audit:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(entry.timestamp, "strftime") else str(entry.timestamp)[:16]
            audit_lines.append(f"  {ts} {entry.action.value}")
        audit_str = "\n".join(audit_lines) if audit_lines else "  (none)"

        return (
            f"**Document Status**\n"
            f"Title: {document.title}\n"
            f"ID: {document_id[:12]}\n"
            f"Status: {document.status.value}\n"
            f"Created: {document.created_at.strftime('%Y-%m-%d %H:%M') if hasattr(document, 'created_at') else 'unknown'}\n\n"
            f"Signers:\n{signers_str}\n\n"
            f"Recent audit:\n{audit_str}"
        )

    def _handle_doc_create(self, args: str, context: dict) -> str:
        """Handle /doc-create <template_id> <title> command."""
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            return "Usage: /doc-create <template_id> <title>"

        template_id = parts[0]
        title = parts[1]

        engine, store = _get_skseal()
        if engine is None:
            return "Error: skseal not installed."

        try:
            from skseal.models import Document, Signer, DocumentStatus

            sender = context.get("sender", "unknown")
            fingerprint = context.get("fingerprint", "")

            document = Document(
                title=title,
                status=DocumentStatus.DRAFT,
                signers=[
                    Signer(
                        name=sender,
                        fingerprint=fingerprint,
                    )
                ],
            )

            store.save_document(document)

            return (
                f"**Document Created**\n"
                f"Title: {title}\n"
                f"ID: {document.document_id[:12]}\n"
                f"Status: draft\n\n"
                f"Add signers and send for signing."
            )

        except Exception as exc:
            return f"Error creating document: {exc}"

    def _handle_doc_list(self, args: str, context: dict) -> str:
        """Handle /doc-list [--status STATUS] command."""
        engine, store = _get_skseal()
        if engine is None:
            return "Error: skseal not installed."

        status_filter = None
        if args.strip():
            parts = args.strip().split()
            if "--status" in parts:
                idx = parts.index("--status")
                if idx + 1 < len(parts):
                    status_filter = parts[idx + 1]

        try:
            from skseal.models import DocumentStatus

            filter_status = None
            if status_filter:
                try:
                    filter_status = DocumentStatus(status_filter)
                except ValueError:
                    return f"Error: Unknown status '{status_filter}'. Valid: draft, pending, partially_signed, completed, voided, expired"

            documents = store.list_documents(status=filter_status)

            if not documents:
                return "No documents found."

            lines = ["**Documents**\n"]
            for doc in documents[:20]:
                signed = sum(1 for s in doc.signers if s.status.value == "signed")
                total = len(doc.signers)
                lines.append(
                    f"  [{doc.document_id[:8]}] {doc.title} — "
                    f"{doc.status.value} ({signed}/{total} signed)"
                )

            return "\n".join(lines)

        except Exception as exc:
            return f"Error listing documents: {exc}"
