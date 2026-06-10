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
