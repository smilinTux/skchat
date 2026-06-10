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
