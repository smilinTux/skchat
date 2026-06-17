from pathlib import Path

from PIL import Image

from skchat.media import detect_mime, is_image, make_thumbnail


def _make_png(p: Path, w=800, h=600):
    Image.new("RGB", (w, h), (10, 120, 200)).save(p, "PNG")


def test_detect_mime_png(tmp_path):
    p = tmp_path / "a.png"
    _make_png(p)
    assert detect_mime(p) == "image/png"


def test_detect_mime_unknown_falls_back(tmp_path):
    p = tmp_path / "weird.zzz"
    p.write_bytes(b"\x00\x01not a known type")
    assert detect_mime(p) == "application/octet-stream"


def test_is_image(tmp_path):
    p = tmp_path / "a.png"
    _make_png(p)
    assert is_image(detect_mime(p)) is True
    assert is_image("application/pdf") is False


def test_make_thumbnail_for_image(tmp_path):
    src = tmp_path / "a.png"
    _make_png(src, 800, 600)
    dst = tmp_path / "thumb.webp"
    assert make_thumbnail(src, dst, max_edge=320) is True
    assert dst.exists()
    with Image.open(dst) as im:
        assert max(im.size) <= 320


def test_make_thumbnail_non_image_returns_false(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"not an image")
    dst = tmp_path / "thumb.webp"
    assert make_thumbnail(src, dst) is False
    assert not dst.exists()
