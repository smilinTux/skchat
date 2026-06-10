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
from .media import detect_mime, is_image, make_thumbnail
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
        return FileRef(
            transfer_id=transfer_id,
            filename=path.name,
            size=path.stat().st_size,
            mime_type=mime,
            sha256=_sha256(path),
            thumbnail_id=thumb_id,
            direction=direction,
        )

    def send_attachment(
        self, recipient: str, path: Path, caption: Optional[str] = None
    ) -> ChatMessage:
        """Send *path* to *recipient* and post an outbound message for it."""
        path = Path(path)
        transfer_id = self._files.send_file(recipient, path)
        ref = self._build_ref(path, transfer_id, direction="sent")
        msg = ChatMessage(
            sender=self._identity,
            recipient=recipient,
            content=caption or "",
            attachments=[ref],
        )
        self._history.save(msg)
        logger.info("sent attachment %s (%s) to %s", ref.filename, transfer_id, recipient)
        return msg
