# Chat File & Image Attachments — Implementation Plan (Spec 1: Core + Web UI)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user drop/paste/pick an image or file in the skchat web window and have it transfer to the peer (encrypted, chunked) and render inline (image thumbnails / file download badges) with progress.

**Architecture:** A surface-agnostic core — `FileRef`/`attachments` on `ChatMessage`, an `AttachmentService` that wraps the existing `FileTransferService` (send → post a message with the FileRef; receive → assemble → post an inbound message), MIME+thumbnail helpers, and a transport selector (skcomms default; WebRTC/Tailscale optional fast-paths, never required) — consumed by the webui (upload endpoint, download/thumb endpoints, inline rendering, drag/drop/paste, WebSocket progress).

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI + HTMX, Pillow (thumbnails), `filetype` (MIME), pytest. Repo: `/home/cbrd21/clawd/skcapstone-repos/skchat`.

**Conventions:** TDD (test first, watch it fail, implement, watch it pass, commit). Run `~/.skenv/bin/python -m pytest`. Explicit `git add` of only your files (never `-A`). Commit messages end with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`. Do NOT push (the orchestrator pushes). Tests are standalone — tmp `~/.skchat` (set `SKCHAT_HOME` or pass dirs), in-process keys, fake transports, NO network/WebRTC/real-tailscale; `conftest.py` already gates desktop notifications off (`SK_DESKTOP_NOTIFY=0`) — leave it.

---

## File structure

- Create `src/skchat/media.py` — MIME detection + thumbnail generation (one responsibility: media inspection).
- Create `src/skchat/attachments.py` — `AttachmentService` + transport selection (the send/receive glue).
- Modify `src/skchat/models.py` — add `FileRef` + `ChatMessage.attachments` + relax the content rule.
- Modify `src/skchat/files.py` — register an `on_complete` callback fired from the `FILE_TRANSFER_DONE` branch of `store_incoming_chunk`.
- Modify `src/skchat/webui.py` — `/upload`, `/file/{id}`, `/file/{id}/thumb`, render attachments, the upload UI + progress, WS `file_progress`.
- Tests: `tests/test_models.py` (extend), `tests/test_media.py` (new), `tests/test_attachments.py` (new), `tests/test_webui_attachments.py` (new).

Dependencies: add `filetype` and `Pillow` to `pyproject.toml` (Task 0).

---

## Task 0: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `Pillow` and `filetype` to the project dependencies**

In `pyproject.toml`, add to the `[project].dependencies` list (or the appropriate optional group if the repo uses extras — match the existing style):
```toml
    "Pillow>=10.0",
    "filetype>=1.2",
```

- [ ] **Step 2: Install into the venv**

Run: `~/.skenv/bin/pip install "Pillow>=10.0" "filetype>=1.2"`
Expected: both install (or already satisfied).

- [ ] **Step 3: Verify import**

Run: `~/.skenv/bin/python -c "import PIL, filetype; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**
```bash
git add pyproject.toml
git commit -m "build: add Pillow + filetype for chat attachment thumbnails/MIME

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1: `FileRef` model + `ChatMessage.attachments`

**Files:**
- Modify: `src/skchat/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_models.py`:
```python
from skchat.models import ChatMessage, ContentType, FileRef


def test_fileref_round_trip():
    ref = FileRef(transfer_id="t1", filename="a.png", size=12,
                  mime_type="image/png", sha256="ab"*32, thumbnail_id="th1",
                  direction="sent")
    assert FileRef(**ref.model_dump()) == ref


def test_message_with_attachment_allows_empty_content():
    msg = ChatMessage(
        sender="capauth:a@skworld.io", recipient="capauth:b@skworld.io",
        content="",
        attachments=[FileRef(transfer_id="t1", filename="a.png", size=1,
                             mime_type="image/png", sha256="x", direction="sent")],
    )
    assert msg.attachments[0].filename == "a.png"
    assert msg.content == ""


def test_message_empty_content_and_no_attachments_rejected():
    import pytest
    with pytest.raises(ValueError):
        ChatMessage(sender="capauth:a@skworld.io",
                    recipient="capauth:b@skworld.io", content="   ")


def test_old_message_json_without_attachments_loads():
    data = {"sender": "capauth:a@skworld.io", "recipient": "capauth:b@skworld.io",
            "content": "hi"}
    msg = ChatMessage(**data)
    assert msg.attachments == []
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_models.py -k "fileref or attachment or old_message" -q`
Expected: FAIL — `ImportError: cannot import name 'FileRef'`.

- [ ] **Step 3: Implement**

In `src/skchat/models.py`: add the import `model_validator` to the existing pydantic import line (`from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator`). Add the `FileRef` model **above** `ChatMessage`:
```python
class FileRef(BaseModel):
    """A file/image attached to a chat message (the transfer it rode on).

    Attributes:
        transfer_id: ID of the underlying FileTransferService transfer.
        filename: Original file name.
        size: Size in bytes.
        mime_type: Detected MIME type (e.g. ``image/png``).
        sha256: Whole-file SHA-256 (hex), for integrity display.
        thumbnail_id: Present for images that have a generated thumbnail
            (equals transfer_id; the thumb is served from the transfer dir).
        direction: ``"sent"`` or ``"received"``.
    """

    transfer_id: str
    filename: str
    size: int
    mime_type: str
    sha256: str
    thumbnail_id: Optional[str] = None
    direction: str = "sent"
```
Add the field to `ChatMessage` (next to `metadata`):
```python
    attachments: list[FileRef] = Field(default_factory=list)
```
Replace the `content_must_not_be_empty` field_validator so empty content is allowed at the field level, and enforce "content OR attachments" at the model level. Delete the existing `@field_validator("content")` block and add **after** the field definitions:
```python
    @model_validator(mode="after")
    def _require_content_or_attachments(self) -> "ChatMessage":
        """A message must carry text content OR at least one attachment."""
        if not (self.content or "").strip() and not self.attachments:
            raise ValueError("Message must have content or at least one attachment")
        return self
```

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_models.py -q`
Expected: PASS (including pre-existing model tests — if a pre-existing test asserted the old "Message content cannot be empty" string, update it to expect the new message; the behavior of rejecting empty+no-attachments is preserved).

- [ ] **Step 5: Commit**
```bash
git add src/skchat/models.py tests/test_models.py
git commit -m "feat(models): FileRef + ChatMessage.attachments (content-or-attachments rule)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: MIME detection + thumbnails (`media.py`)

**Files:**
- Create: `src/skchat/media.py`
- Test: `tests/test_media.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_media.py`:
```python
from pathlib import Path

from PIL import Image

from skchat.media import detect_mime, make_thumbnail, is_image


def _make_png(p: Path, w=800, h=600):
    Image.new("RGB", (w, h), (10, 120, 200)).save(p, "PNG")


def test_detect_mime_png(tmp_path):
    p = tmp_path / "a.png"; _make_png(p)
    assert detect_mime(p) == "image/png"


def test_detect_mime_unknown_falls_back(tmp_path):
    p = tmp_path / "weird.xyz"; p.write_bytes(b"\x00\x01not a known type")
    assert detect_mime(p) == "application/octet-stream"


def test_is_image(tmp_path):
    p = tmp_path / "a.png"; _make_png(p)
    assert is_image(detect_mime(p)) is True
    assert is_image("application/pdf") is False


def test_make_thumbnail_for_image(tmp_path):
    src = tmp_path / "a.png"; _make_png(src, 800, 600)
    dst = tmp_path / "thumb.webp"
    assert make_thumbnail(src, dst, max_edge=320) is True
    assert dst.exists()
    with Image.open(dst) as im:
        assert max(im.size) <= 320


def test_make_thumbnail_non_image_returns_false(tmp_path):
    src = tmp_path / "f.bin"; src.write_bytes(b"not an image")
    dst = tmp_path / "thumb.webp"
    assert make_thumbnail(src, dst) is False
    assert not dst.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_media.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.media'`.

- [ ] **Step 3: Implement**

Create `src/skchat/media.py`:
```python
"""MIME detection + thumbnail generation for chat attachments."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

logger = logging.getLogger(__name__)

# Decompression-bomb guard for Pillow (None = unlimited, which we never want).
_MAX_IMAGE_PIXELS = 64_000_000  # ~64 MP


def detect_mime(path: Path) -> str:
    """Detect a file's MIME type from magic bytes, falling back to extension.

    Returns ``application/octet-stream`` when unknown.
    """
    try:
        import filetype

        kind = filetype.guess(str(path))
        if kind is not None:
            return kind.mime
    except Exception as exc:  # noqa: BLE001
        logger.debug("filetype guess failed for %s: %s", path, exc)
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def is_image(mime_type: str) -> bool:
    """True if the MIME type is an image we can thumbnail/preview."""
    return mime_type.startswith("image/")


def make_thumbnail(src: Path, dst: Path, max_edge: int = 320) -> bool:
    """Write a WebP thumbnail (<= max_edge on the long side) for an image.

    Returns True on success, False if *src* is not a decodable image (callers
    fall back to a generic file badge). Never raises.
    """
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
        with Image.open(src) as im:
            im.verify()  # cheap bomb/format check
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge))
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, "WEBP", quality=80)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure → no thumbnail
        logger.debug("thumbnail failed for %s: %s", src, exc)
        return False
```

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_media.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**
```bash
git add src/skchat/media.py tests/test_media.py
git commit -m "feat(media): MIME detection + WebP thumbnails (bomb-guarded)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: `AttachmentService.send_attachment`

**Files:**
- Create: `src/skchat/attachments.py`
- Test: `tests/test_attachments.py`

Notes for the implementer: `FileTransferService(identity).send_file(recipient, Path) -> transfer_id: str` already chunks+encrypts+sends over skcomms and persists state under `~/.skchat/transfers/`. `ChatHistory(history_dir=...).save(ChatMessage)` appends to dated JSONL. Inject both so tests use fakes/tmp dirs.

- [ ] **Step 1: Write failing tests**

Create `tests/test_attachments.py`:
```python
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from skchat.attachments import AttachmentService
from skchat.history import ChatHistory


def _png(p: Path):
    Image.new("RGB", (40, 30), (1, 2, 3)).save(p, "PNG")


def _service(tmp_path):
    fake_transfer = MagicMock()
    fake_transfer.send_file.return_value = "tid-123"     # transfer_id
    history = ChatHistory(history_dir=tmp_path / "history")
    svc = AttachmentService(
        identity="capauth:me@skworld.io",
        history=history,
        file_service=fake_transfer,
        thumb_root=tmp_path / "thumbs",
    )
    return svc, fake_transfer, history


def test_send_attachment_posts_message_with_fileref(tmp_path):
    svc, fake_transfer, history = _service(tmp_path)
    f = tmp_path / "doc.pdf"; f.write_bytes(b"%PDF-1.4 hello")
    msg = svc.send_attachment("capauth:peer@skworld.io", f, caption="see this")
    fake_transfer.send_file.assert_called_once()
    assert msg.sender == "capauth:me@skworld.io"
    assert msg.content == "see this"
    assert len(msg.attachments) == 1
    ref = msg.attachments[0]
    assert ref.transfer_id == "tid-123"
    assert ref.filename == "doc.pdf"
    assert ref.mime_type == "application/pdf"
    assert ref.direction == "sent"
    assert ref.thumbnail_id is None
    # persisted
    assert any(m.id == msg.id for m in history.load(limit=10))


def test_send_image_attachment_generates_thumbnail(tmp_path):
    svc, _, _ = _service(tmp_path)
    f = tmp_path / "pic.png"; _png(f)
    msg = svc.send_attachment("capauth:peer@skworld.io", f)
    ref = msg.attachments[0]
    assert ref.mime_type == "image/png"
    assert ref.thumbnail_id == "tid-123"
    assert (tmp_path / "thumbs" / "tid-123" / "thumb.webp").exists()


def test_send_attachment_sha256_matches_file(tmp_path):
    svc, _, _ = _service(tmp_path)
    f = tmp_path / "doc.bin"; f.write_bytes(b"abc123")
    msg = svc.send_attachment("capauth:peer@skworld.io", f)
    assert msg.attachments[0].sha256 == hashlib.sha256(b"abc123").hexdigest()
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_attachments.py -k send -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.attachments'`.

- [ ] **Step 3: Implement**

Create `src/skchat/attachments.py`:
```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_attachments.py -k send -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add src/skchat/attachments.py tests/test_attachments.py
git commit -m "feat(attachments): AttachmentService.send_attachment posts message + FileRef

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Receive wiring — `on_transfer_complete` + `files.py` callback

**Files:**
- Modify: `src/skchat/files.py` (register + fire an `on_complete` callback in the `FILE_TRANSFER_DONE` branch of `store_incoming_chunk`)
- Modify: `src/skchat/attachments.py` (`on_transfer_complete`)
- Test: `tests/test_attachments.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_attachments.py`:
```python
def test_on_transfer_complete_posts_inbound_message(tmp_path):
    # a real-ish received file on disk; fake file_service.receive_file returns it
    received = tmp_path / "incoming" / "photo.png"
    received.parent.mkdir(parents=True)
    _png(received)
    fake_transfer = MagicMock()
    fake_transfer.receive_file.return_value = received
    history = ChatHistory(history_dir=tmp_path / "h")
    svc = AttachmentService("capauth:me@skworld.io", history, fake_transfer,
                            thumb_root=tmp_path / "t")
    msg = svc.on_transfer_complete(
        transfer_id="tid-9", sender="capauth:peer@skworld.io")
    assert msg is not None
    assert msg.sender == "capauth:peer@skworld.io"
    assert msg.recipient == "capauth:me@skworld.io"
    assert msg.attachments[0].transfer_id == "tid-9"
    assert msg.attachments[0].direction == "received"
    assert msg.attachments[0].mime_type == "image/png"
    assert msg.attachments[0].thumbnail_id == "tid-9"
    assert any(m.id == msg.id for m in history.load(limit=10))


def test_files_fires_on_complete_callback():
    from skchat.files import FileTransferService
    called = {}
    svc = FileTransferService("capauth:me@skworld.io")
    svc.on_complete = lambda transfer_id, sender: called.setdefault("args", (transfer_id, sender))
    # simulate a DONE message arriving
    svc.store_incoming_chunk({"type": "FILE_TRANSFER_DONE", "transfer_id": "tid-7",
                              "sender": "capauth:peer@skworld.io"})
    assert called.get("args", (None, None))[0] == "tid-7"
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_attachments.py -k "on_transfer_complete or fires_on_complete" -q`
Expected: FAIL — `AttributeError: 'AttachmentService' object has no attribute 'on_transfer_complete'` and the callback not fired.

- [ ] **Step 3: Implement**

In `src/skchat/files.py`:
- In `FileTransferService.__init__`, add `self.on_complete: Optional[Callable[[str, str], None]] = None` (import `Callable`, `Optional` as needed).
- In `store_incoming_chunk`, inside the `elif msg_type == "FILE_TRANSFER_DONE":` branch (~line 690), after the existing completion handling, add:
```python
            if self.on_complete is not None:
                try:
                    self.on_complete(msg.get("transfer_id", ""), msg.get("sender", ""))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("on_complete callback failed: %s", exc)
```
(Ensure `files.py` has a module `logger` — it does after the earlier logger-hardening fix; if not, add `logger = logging.getLogger(__name__)`.)

In `src/skchat/attachments.py`, add to `AttachmentService`:
```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_attachments.py -q`
Expected: PASS (all attachment tests). Then run `~/.skenv/bin/python -m pytest tests/test_files.py -q` to confirm no regression in the file-transfer suite.

- [ ] **Step 5: Commit**
```bash
git add src/skchat/files.py src/skchat/attachments.py tests/test_attachments.py
git commit -m "feat(attachments): receive wiring — on_complete callback posts inbound message

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Transport selection (default skcomms; optional fast-paths)

**Files:**
- Modify: `src/skchat/attachments.py` (a `_select_transport` + `transport` arg on `send_attachment`)
- Test: `tests/test_attachments.py`

Architecture note: the default `send_file` path (skcomms) MUST work with zero fast-paths. WebRTC/Tailscale are *probes* — if a probe says "available", use it; otherwise fall back to skcomms. **No path may require tailscale.**

- [ ] **Step 1: Write failing tests**

Add to `tests/test_attachments.py`:
```python
def test_transport_auto_falls_back_to_skcomm_when_no_fastpath(tmp_path):
    svc, fake_transfer, _ = _service(tmp_path)
    f = tmp_path / "d.bin"; f.write_bytes(b"x")
    # no webrtc, no tailscale available
    chosen = svc._select_transport("capauth:peer@skworld.io", "auto",
                                    webrtc_ok=lambda r: False, tailscale_ok=lambda r: False)
    assert chosen == "skcomms"


def test_transport_auto_prefers_webrtc_then_tailscale(tmp_path):
    svc, _, _ = _service(tmp_path)
    assert svc._select_transport("r", "auto", webrtc_ok=lambda r: True,
                                 tailscale_ok=lambda r: True) == "webrtc"
    assert svc._select_transport("r", "auto", webrtc_ok=lambda r: False,
                                 tailscale_ok=lambda r: True) == "tailscale"


def test_transport_explicit_override(tmp_path):
    svc, _, _ = _service(tmp_path)
    assert svc._select_transport("r", "skcomms", webrtc_ok=lambda r: True,
                                 tailscale_ok=lambda r: True) == "skcomms"
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_attachments.py -k transport -q`
Expected: FAIL — `AttributeError: ... '_select_transport'`.

- [ ] **Step 3: Implement**

In `src/skchat/attachments.py`, add to `AttachmentService` and use it in `send_attachment`:
```python
    def _webrtc_available(self, recipient: str) -> bool:
        """Best-effort: is a live WebRTC data channel open to recipient?"""
        try:
            checker = getattr(self._files, "webrtc_channel_open", None)
            return bool(checker(recipient)) if callable(checker) else False
        except Exception:  # noqa: BLE001
            return False

    def _tailscale_available(self, recipient: str) -> bool:
        """Best-effort: does the peer have a reachable tailscale address?

        NEVER required — returns False on any uncertainty so we fall back.
        """
        try:
            checker = getattr(self._files, "tailscale_addr", None)
            return bool(checker(recipient)) if callable(checker) else False
        except Exception:  # noqa: BLE001
            return False

    def _select_transport(self, recipient, transport="auto", webrtc_ok=None, tailscale_ok=None):
        """Pick a transport. 'auto' tries fast-paths, falls back to skcomm."""
        if transport != "auto":
            return transport
        webrtc_ok = webrtc_ok or self._webrtc_available
        tailscale_ok = tailscale_ok or self._tailscale_available
        if webrtc_ok(recipient):
            return "webrtc"
        if tailscale_ok(recipient):
            return "tailscale"
        return "skcomms"
```
Update `send_attachment` to accept `transport: str = "auto"`, compute `chosen = self._select_transport(recipient, transport)`, and dispatch: for `"webrtc"` call `self._files.send_file_p2p(recipient, path)` if available, else fall back to `send_file`; `"tailscale"` likewise falls back to `send_file` if no tailscale sender exists yet (Tailscale direct-send is a future enhancement — the selector is in place, the default still ships). Record the chosen transport in `msg.metadata["transport"] = chosen`. Always return a valid transfer_id (fall back to `send_file` if a fast-path call raises).

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_attachments.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/skchat/attachments.py tests/test_attachments.py
git commit -m "feat(attachments): transport selector — skcomms default, WebRTC/Tailscale optional

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: webui `/upload` endpoint

**Files:**
- Modify: `src/skchat/webui.py`
- Test: `tests/test_webui_attachments.py`

Notes: webui exposes `app` (FastAPI), `_get_identity() -> str`, a history accessor (around line 210, returns `ChatHistory()`), and `_ws_broadcast(dict)`. Use FastAPI `UploadFile`, `Form`. Stage uploads under `~/.skchat/uploads/<uuid>/<filename>`. Build the `AttachmentService` per-request (or a cached singleton) with a real `FileTransferService(_get_identity())`.

- [ ] **Step 1: Write failing test**

Create `tests/test_webui_attachments.py`:
```python
import io
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from skchat import webui


def _png_bytes():
    buf = io.BytesIO(); Image.new("RGB", (20, 20), (5, 5, 5)).save(buf, "PNG")
    return buf.getvalue()


def test_upload_sends_and_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))  # sandbox ~/.skchat
    sent = {}

    class FakeAttach:
        def send_attachment(self, recipient, path, caption=None):
            sent["recipient"] = recipient
            sent["name"] = path.name
            from skchat.models import ChatMessage, FileRef
            return ChatMessage(sender="capauth:me@skworld.io", recipient=recipient,
                               content=caption or "",
                               attachments=[FileRef(transfer_id="tid", filename=path.name,
                                   size=path.stat().st_size, mime_type="image/png",
                                   sha256="x", direction="sent")])

    with patch.object(webui, "_attachment_service", return_value=FakeAttach()):
        client = TestClient(webui.app)
        r = client.post("/upload",
                        data={"recipient": "capauth:peer@skworld.io", "caption": "hi"},
                        files={"file": ("pic.png", _png_bytes(), "image/png")})
    assert r.status_code == 200
    assert sent["recipient"] == "capauth:peer@skworld.io"
    assert sent["name"] == "pic.png"


def test_upload_over_size_cap_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setattr(webui, "MAX_UPLOAD_BYTES", 10, raising=False)
    client = TestClient(webui.app)
    r = client.post("/upload",
                    data={"recipient": "capauth:peer@skworld.io"},
                    files={"file": ("big.bin", b"x" * 100, "application/octet-stream")})
    assert r.status_code == 413
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k upload -q`
Expected: FAIL — 404 (no `/upload` route) / `_attachment_service` missing.

- [ ] **Step 3: Implement**

In `src/skchat/webui.py` add near the top (after imports): `MAX_UPLOAD_BYTES = 100 * 1024 * 1024` and a helper:
```python
def _attachment_service():
    """Build an AttachmentService bound to a real FileTransferService."""
    from .attachments import AttachmentService
    from .files import FileTransferService
    ident = _get_identity()
    fs = FileTransferService(ident)
    return AttachmentService(ident, _get_history(), fs)
```
(Use the existing history accessor name — if it's an inline `ChatHistory()` at ~line 210, extract it to `_get_history()` and reuse.) Add the endpoint:
```python
from fastapi import UploadFile, File, Form, HTTPException
from pathlib import Path
import uuid as _uuid

@app.post("/upload")
async def upload(recipient: str = Form(...),
                 caption: str = Form(""),
                 file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    home = Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat")))
    staged = home / "uploads" / _uuid.uuid4().hex / (file.filename or "upload.bin")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    svc = _attachment_service()
    msg = svc.send_attachment(recipient, staged, caption=caption or None)
    import asyncio
    asyncio.create_task(_ws_broadcast({"type": "new"}))
    return {"id": msg.id, "transfer_id": msg.attachments[0].transfer_id,
            "filename": msg.attachments[0].filename}
```
(Reuse the existing `import os` / `asyncio` if already imported at module top.)

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k upload -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**
```bash
git add src/skchat/webui.py tests/test_webui_attachments.py
git commit -m "feat(webui): POST /upload — multipart file → AttachmentService (100MB cap)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: webui `/file/{transfer_id}` + `/thumb` download endpoints

**Files:**
- Modify: `src/skchat/webui.py`
- Test: `tests/test_webui_attachments.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_webui_attachments.py`:
```python
from pathlib import Path
from fastapi.testclient import TestClient
from skchat import webui


def test_download_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    rec = tmp_path / "received" / "tid-1"; rec.mkdir(parents=True)
    (rec / "a.txt").write_bytes(b"hello world")
    client = TestClient(webui.app)
    r = client.get("/file/tid-1")
    assert r.status_code == 200
    assert r.content == b"hello world"
    assert "attachment" in r.headers.get("content-disposition", "")


def test_download_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    client = TestClient(webui.app)
    r = client.get("/file/..%2f..%2fetc%2fpasswd")
    assert r.status_code in (400, 404)


def test_thumb_404_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    client = TestClient(webui.app)
    assert client.get("/file/nope/thumb").status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k "download or thumb" -q`
Expected: FAIL (404 / route missing).

- [ ] **Step 3: Implement**

In `src/skchat/webui.py`:
```python
import re as _re
from fastapi.responses import FileResponse

_TID_RE = _re.compile(r"^[A-Za-z0-9._-]+$")  # no slashes / dotdot escapes

def _skchat_home() -> Path:
    return Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat")))

def _safe_transfer_dir(transfer_id: str, sub: str) -> Optional[Path]:
    if not _TID_RE.match(transfer_id):
        return None
    base = (_skchat_home() / sub).resolve()
    target = (base / transfer_id).resolve()
    if base not in target.parents and target != base:
        return None
    return target if target.exists() else None

@app.get("/file/{transfer_id}")
def download_file(transfer_id: str):
    d = _safe_transfer_dir(transfer_id, "received") or _safe_transfer_dir(transfer_id, "uploads")
    if d is None:
        raise HTTPException(status_code=404, detail="not found")
    files = [p for p in d.rglob("*") if p.is_file() and p.name != "thumb.webp"]
    if not files:
        raise HTTPException(status_code=404, detail="empty")
    f = files[0]
    return FileResponse(str(f), filename=f.name,
                        headers={"Content-Disposition": f'attachment; filename="{f.name}"'})

@app.get("/file/{transfer_id}/thumb")
def file_thumb(transfer_id: str):
    for sub in ("received", "thumbnails", "uploads"):
        d = _safe_transfer_dir(transfer_id, sub)
        if d and (d / "thumb.webp").exists():
            return FileResponse(str(d / "thumb.webp"), media_type="image/webp")
    raise HTTPException(status_code=404, detail="no thumbnail")
```
(`_TID_RE` rejects `..` and `/`; `_safe_transfer_dir` resolves and confirms the path stays under the base — path-traversal guard. Use the existing `Optional` import.)

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k "download or thumb" -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**
```bash
git add src/skchat/webui.py tests/test_webui_attachments.py
git commit -m "feat(webui): /file/{id} download + /thumb (path-traversal guarded)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: webui rendering — inline images + file badges

**Files:**
- Modify: `src/skchat/webui.py` (`_render_messages`)
- Test: `tests/test_webui_attachments.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_webui_attachments.py`:
```python
from skchat.models import ChatMessage, FileRef
from skchat import webui


def _msg(**kw):
    base = dict(sender="capauth:peer@skworld.io", recipient="capauth:me@skworld.io",
                content="")
    base.update(kw)
    return ChatMessage(**base)


class _Hist:
    def __init__(self, msgs): self._m = msgs
    def load(self, **kw): return self._m


def test_render_inline_image(monkeypatch):
    m = _msg(attachments=[FileRef(transfer_id="tid-img", filename="p.png", size=3,
             mime_type="image/png", sha256="x", thumbnail_id="tid-img", direction="received")])
    html = webui._render_messages(_Hist([m]), "capauth:me@skworld.io")
    assert "/file/tid-img/thumb" in html
    assert "/file/tid-img" in html  # full link
    assert "<img" in html


def test_render_file_badge(monkeypatch):
    m = _msg(attachments=[FileRef(transfer_id="tid-doc", filename="r.pdf", size=2048,
             mime_type="application/pdf", sha256="x", direction="received")])
    html = webui._render_messages(_Hist([m]), "capauth:me@skworld.io")
    assert "/file/tid-doc" in html
    assert "r.pdf" in html
    assert "<img" not in html.split("tid-doc")[0][-80:]  # no inline image for pdf
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k render -q`
Expected: FAIL (attachments not rendered).

- [ ] **Step 3: Implement**

In `_render_messages` (webui.py), when building each message's HTML, after the text/content block, append attachment markup. For each `att` in `message.attachments`:
```python
        for att in getattr(message, "attachments", []) or []:
            if att.mime_type.startswith("image/") and att.thumbnail_id:
                parts.append(
                    f'<a href="/file/{att.transfer_id}" target="_blank">'
                    f'<img class="att-img" src="/file/{att.transfer_id}/thumb" '
                    f'alt="{att.filename}" loading="lazy"></a>'
                )
            else:
                kb = max(1, att.size // 1024)
                parts.append(
                    f'<a class="att-file" href="/file/{att.transfer_id}">'
                    f'📄 {att.filename} · {kb} KB · {att.mime_type}</a>'
                )
```
(Match the existing string-building style in `_render_messages` — `parts` is illustrative; integrate into however the function accumulates HTML.)

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k render -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**
```bash
git add src/skchat/webui.py tests/test_webui_attachments.py
git commit -m "feat(webui): render inline image thumbnails + file download badges

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: webui frontend — file button + drag-drop + paste + progress

**Files:**
- Modify: `src/skchat/webui.py` (the `/legacy` embedded HTML/JS + a `file_progress` WS forward)
- Test: `tests/test_webui_attachments.py`

This task is mostly browser JS (not unit-testable headless), so the test asserts the served HTML wires the upload affordances; the JS itself is reviewed by inspection + the manual smoke at the end.

- [ ] **Step 1: Write failing test**

Add to `tests/test_webui_attachments.py`:
```python
from fastapi.testclient import TestClient
from skchat import webui

def test_legacy_html_has_upload_affordances():
    html = TestClient(webui.app).get("/legacy").text
    assert 'type="file"' in html
    assert "/upload" in html
    assert "drop" in html.lower() or "dragover" in html.lower()
    assert "paste" in html.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k affordances -q`
Expected: FAIL (no file input in HTML).

- [ ] **Step 3: Implement**

In the `/legacy` HTML string, extend the chat form + add JS:
```html
<form id="bar" hx-post="/send" hx-target="#chat" hx-swap="innerHTML"
      hx-on::after-request="this.querySelector('input[type=text]').value=''">
  <select name="recipient" id="recipient-sel"></select>
  <input type="text" name="content" placeholder="Message…" autocomplete="off">
  <input type="file" id="file-input" multiple style="display:none">
  <button type="button" id="attach-btn" title="Attach">📎</button>
  <button type="submit">Send</button>
</form>
<div id="upload-progress" style="display:none"><progress id="up-bar" max="100" value="0"></progress> <span id="up-label"></span></div>
<script>
const fi = document.getElementById('file-input');
document.getElementById('attach-btn').onclick = () => fi.click();
function recipient(){ return document.getElementById('recipient-sel').value; }
async function uploadFiles(files){
  const prog = document.getElementById('upload-progress');
  const bar = document.getElementById('up-bar'), label = document.getElementById('up-label');
  for (const f of files){
    prog.style.display='block'; bar.value=0; label.textContent='Uploading '+f.name+'…';
    const fd = new FormData(); fd.append('recipient', recipient());
    fd.append('caption', document.querySelector('input[name=content]').value||'');
    fd.append('file', f);
    await new Promise((res)=>{ const xhr=new XMLHttpRequest(); xhr.open('POST','/upload');
      xhr.upload.onprogress=e=>{ if(e.lengthComputable) bar.value=Math.round(100*e.loaded/e.total); };
      xhr.onload=()=>{ res(); }; xhr.onerror=()=>res(); xhr.send(fd); });
  }
  prog.style.display='none'; htmx.ajax('GET','/messages','#chat');
}
fi.onchange = () => { if (fi.files.length) uploadFiles(fi.files); fi.value=''; };
const chat = document.getElementById('chat');
['dragover','drop'].forEach(ev=>chat.addEventListener(ev,e=>{e.preventDefault();}));
chat.addEventListener('drop', e=>{ if(e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });
document.addEventListener('paste', e=>{ const items=[...(e.clipboardData?.items||[])];
  const imgs=items.filter(i=>i.type.startsWith('image/')).map(i=>i.getAsFile()).filter(Boolean);
  if(imgs.length) uploadFiles(imgs); });
</script>
```
Add a `.att-img{max-width:240px;border-radius:8px}` style. In the `/ws/chat` handler, when broadcasting, also forward any `{type:"file_progress",...}` messages pushed via `_ws_broadcast` (the upload itself reports progress client-side via XHR, so server-side progress is optional for v1 — the XHR `upload.onprogress` already drives the bar).

- [ ] **Step 4: Run to verify pass**

Run: `~/.skenv/bin/python -m pytest tests/test_webui_attachments.py -k affordances -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add src/skchat/webui.py tests/test_webui_attachments.py
git commit -m "feat(webui): file button + drag-drop + paste-image upload UI + progress

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Full-suite verification + daemon wiring

**Files:**
- Modify: `src/skchat/daemon.py` (bind AttachmentService.on_complete so received files post messages in the running daemon)
- Test: full suite

- [ ] **Step 1: Wire the daemon**

In `daemon.py`, where the `FileTransferService` is constructed for receiving (search `FileTransferService(`), build an `AttachmentService` with the same identity + the daemon's `ChatHistory` and call `attach.bind(file_service)` so `on_complete` posts inbound messages. If the daemon doesn't already hold a `FileTransferService` instance for receive, add one and route `store_incoming_chunk` to it. Keep it behind the existing transport-init try/except so a failure is non-fatal.

- [ ] **Step 2: Run the FULL suite**

Run: `~/.skenv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all prior tests + the new ones PASS (19 pre-existing skips remain; the 6 `skref`-missing uninstall_wizard failures are pre-existing and unrelated — do not chase them here).

- [ ] **Step 3: Manual smoke (document, do not block CI)**

Note in the commit body the manual check: start the webui (`skchat webui` or the documented launch), open `/legacy`, drag an image in → it appears inline; send to a peer with a running daemon → it lands in their `~/.skchat/received/` and renders. (This is a manual verification, recorded for the reviewer.)

- [ ] **Step 4: Commit**
```bash
git add src/skchat/daemon.py
git commit -m "feat(daemon): bind AttachmentService — received files post chat messages

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-review notes (for the implementer)

- If a pre-existing `test_models.py` test asserted the exact old error string "Message content cannot be empty", update it to the new model-validator message — the *behavior* (reject empty + no attachments) is unchanged.
- `_get_history()` may not exist yet in webui — if history is fetched inline at ~line 210, extract it to a `_get_history()` helper in Task 6 and reuse it in Tasks 6–8.
- Every new test uses tmp dirs / `SKCHAT_HOME` / fakes — none touch the real `~/.skchat`, the network, WebRTC, or tailscale. The transport selector is exercised purely through injected probes.
- Tailscale is only ever a *probe that can say no* — there is no code path that requires it (verified by `test_transport_auto_falls_back_to_skcomm_when_no_fastpath`).
