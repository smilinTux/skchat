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
    fake_transfer.send_file.return_value = "tid-123"  # transfer_id
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
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 hello")
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
    f = tmp_path / "pic.png"
    _png(f)
    msg = svc.send_attachment("capauth:peer@skworld.io", f)
    ref = msg.attachments[0]
    assert ref.mime_type == "image/png"
    assert ref.thumbnail_id == "tid-123"
    assert (tmp_path / "thumbs" / "tid-123" / "thumb.webp").exists()


def test_send_attachment_sha256_matches_file(tmp_path):
    svc, _, _ = _service(tmp_path)
    f = tmp_path / "doc.bin"
    f.write_bytes(b"abc123")
    msg = svc.send_attachment("capauth:peer@skworld.io", f)
    assert msg.attachments[0].sha256 == hashlib.sha256(b"abc123").hexdigest()


def test_on_transfer_complete_posts_inbound_message(tmp_path):
    # a real-ish received file on disk; fake file_service.receive_file returns it
    received = tmp_path / "incoming" / "photo.png"
    received.parent.mkdir(parents=True)
    _png(received)
    fake_transfer = MagicMock()
    fake_transfer.receive_file.return_value = received
    history = ChatHistory(history_dir=tmp_path / "h")
    svc = AttachmentService(
        "capauth:me@skworld.io", history, fake_transfer, thumb_root=tmp_path / "t"
    )
    msg = svc.on_transfer_complete(transfer_id="tid-9", sender="capauth:peer@skworld.io")
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
    svc.store_incoming_chunk(
        {"type": "FILE_TRANSFER_DONE", "transfer_id": "tid-7", "sender": "capauth:peer@skworld.io"}
    )
    assert called.get("args", (None, None))[0] == "tid-7"


def test_transport_auto_falls_back_to_skcomms_when_no_fastpath(tmp_path):
    svc, fake_transfer, _ = _service(tmp_path)
    f = tmp_path / "d.bin"
    f.write_bytes(b"x")
    # no webrtc, no tailscale available
    chosen = svc._select_transport(
        "capauth:peer@skworld.io", "auto", webrtc_ok=lambda r: False, tailscale_ok=lambda r: False
    )
    assert chosen == "skcomms"


def test_transport_auto_prefers_webrtc_then_tailscale(tmp_path):
    svc, _, _ = _service(tmp_path)
    assert (
        svc._select_transport("r", "auto", webrtc_ok=lambda r: True, tailscale_ok=lambda r: True)
        == "webrtc"
    )
    assert (
        svc._select_transport("r", "auto", webrtc_ok=lambda r: False, tailscale_ok=lambda r: True)
        == "tailscale"
    )


def test_transport_explicit_override(tmp_path):
    svc, _, _ = _service(tmp_path)
    assert (
        svc._select_transport(
            "r", "skcomms", webrtc_ok=lambda r: True, tailscale_ok=lambda r: True
        )
        == "skcomms"
    )
