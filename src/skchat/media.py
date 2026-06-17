"""MIME detection + thumbnail generation for chat attachments."""

from __future__ import annotations

import logging
import mimetypes
import shutil
import subprocess
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


def make_video_thumbnail(src: Path, dst: Path, max_edge: int = 320) -> bool:
    """Write a WebP thumbnail (first frame, <= max_edge wide) for a video.

    Grabs a single frame with ffmpeg and scales it so the long edge is at most
    *max_edge* (aspect preserved). Mirrors :func:`make_thumbnail`'s contract:
    returns True on success, False if ffmpeg is missing or fails. Never raises.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.debug("ffmpeg not found; skipping video thumbnail for %s", src)
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # scale: clamp the long edge to max_edge while keeping aspect ratio
        # (-2 keeps the other dimension even, required by some encoders).
        scale = (
            f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':'if(gt(iw,ih),-2,min({max_edge},ih))'"
        )
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                scale,
                str(dst),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0 and dst.exists()
    except Exception as exc:  # noqa: BLE001 - any failure → no thumbnail
        logger.debug("video thumbnail failed for %s: %s", src, exc)
        return False
