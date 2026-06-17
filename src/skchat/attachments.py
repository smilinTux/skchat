"""AttachmentService — bridges file transfers and chat messages.

Sending: kick off the (encrypted, chunked) FileTransferService send AND post a
ChatMessage carrying a FileRef, so the file shows up *in the conversation*.
Receiving: on transfer completion, assemble + thumbnail + post an inbound
ChatMessage. Surface-agnostic — webui/CLI/TUI all call this.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

from .history import ChatHistory
from .media import detect_mime, is_image, make_thumbnail, make_video_thumbnail
from .models import ChatMessage, FileRef

logger = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class AttachmentService:
    """Send/receive files as chat-message attachments.

    Args:
        identity: this agent's CapAuth URI (the sender of outbound messages).
        history: ChatHistory to persist messages into.
        file_service: a FileTransferService (or compatible) exposing
            ``send_file(recipient, Path) -> transfer_id`` and
            ``receive_file(transfer_id) -> Optional[Path]``.
        thumb_root: directory under which per-transfer thumbnails are written
            (defaults to ``~/.skchat/thumbnails``).
    """

    def __init__(
        self,
        identity: str,
        history: ChatHistory,
        file_service: Any,
        thumb_root: Optional[Path] = None,
    ) -> None:
        self._identity = identity
        self._history = history
        self._files = file_service
        self._thumb_root = Path(thumb_root or (Path.home() / ".skchat" / "thumbnails"))

    def _build_ref(self, path: Path, transfer_id: str, direction: str) -> FileRef:
        mime = detect_mime(path)
        thumb_id = None
        if is_image(mime):
            dst = self._thumb_root / transfer_id / "thumb.webp"
            if make_thumbnail(path, dst):
                thumb_id = transfer_id
        elif mime.startswith("video/"):
            dst = self._thumb_root / transfer_id / "thumb.webp"
            if make_video_thumbnail(path, dst):
                thumb_id = transfer_id
        return FileRef(
            transfer_id=transfer_id,
            filename=path.name,
            size=path.stat().st_size,
            mime_type=mime,
            sha256=_sha256(path),
            thumbnail_id=thumb_id,
            direction=direction,
        )

    def _real_method(self, name: str):
        """Return a genuinely-declared method on the file_service, else None.

        Looks the attribute up on the *type* so that test doubles (e.g. a bare
        ``MagicMock`` that auto-vivifies every attribute access) do not spuriously
        advertise fast-path capabilities. Fast-paths are NEVER required.
        """
        if hasattr(type(self._files), name):
            checker = getattr(self._files, name, None)
            return checker if callable(checker) else None
        return None

    def _webrtc_available(self, recipient: str) -> bool:
        """Best-effort: is a live WebRTC data channel open to recipient?"""
        try:
            checker = self._real_method("webrtc_channel_open")
            return bool(checker(recipient)) if checker is not None else False
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "webrtc availability check failed for %s (%s: %s); assuming unavailable",
                recipient,
                type(exc).__name__,
                exc,
            )
            return False

    def _tailscale_available(self, recipient: str) -> bool:
        """Best-effort: does the peer have a reachable tailscale address?

        NEVER required — returns False on any uncertainty so we fall back.
        """
        try:
            checker = self._real_method("tailscale_addr")
            return bool(checker(recipient)) if checker is not None else False
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "tailscale availability check failed for %s (%s: %s); assuming unavailable",
                recipient,
                type(exc).__name__,
                exc,
            )
            return False

    def _select_transport(self, recipient, transport="auto", webrtc_ok=None, tailscale_ok=None):
        """Pick a transport. 'auto' tries fast-paths, falls back to skcomms (default)."""
        if transport != "auto":
            return transport
        webrtc_ok = webrtc_ok or self._webrtc_available
        tailscale_ok = tailscale_ok or self._tailscale_available
        if webrtc_ok(recipient):
            return "webrtc"
        if tailscale_ok(recipient):
            return "tailscale"
        return "skcomms"

    def send_attachment(
        self,
        recipient: str,
        path: Path,
        caption: Optional[str] = None,
        transport: str = "auto",
    ) -> ChatMessage:
        """Send *path* to *recipient* and post an outbound message for it."""
        path = Path(path)
        chosen = self._select_transport(recipient, transport)
        transfer_id: Optional[str] = None
        if chosen == "webrtc":
            sender = self._real_method("send_file_p2p")
            if sender is not None:
                try:
                    transfer_id = sender(recipient, path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "webrtc send_file_p2p failed (%s), falling back to skcomms", exc
                    )
                    transfer_id = None
        # tailscale direct-send is a future enhancement; falls through to skcomms.
        if transfer_id is None:
            transfer_id = self._files.send_file(recipient, path)
        ref = self._build_ref(path, transfer_id, direction="sent")
        msg = ChatMessage(
            sender=self._identity,
            recipient=recipient,
            content=caption or "",
            attachments=[ref],
            metadata={"transport": chosen},
        )
        self._history.save(msg)
        logger.info(
            "sent attachment %s (%s) to %s via %s",
            ref.filename,
            transfer_id,
            recipient,
            chosen,
        )
        return msg

    def on_transfer_complete(self, transfer_id: str, sender: str) -> Optional[ChatMessage]:
        """Called when an inbound transfer finishes: assemble + post a message."""
        path = self._files.receive_file(transfer_id)
        if path is None:
            logger.warning("transfer %s completed but no file assembled", transfer_id)
            return None
        path = Path(path)
        ref = self._build_ref(path, transfer_id, direction="received")
        msg = ChatMessage(
            sender=sender or "capauth:unknown@skworld.io",
            recipient=self._identity,
            content="",
            attachments=[ref],
        )
        self._history.save(msg)
        logger.info("received attachment %s (%s) from %s", ref.filename, transfer_id, sender)
        return msg

    def bind(self, file_service: Any = None) -> None:
        """Wire this service's on_transfer_complete into a FileTransferService."""
        target = file_service or self._files
        target.on_complete = self.on_transfer_complete
