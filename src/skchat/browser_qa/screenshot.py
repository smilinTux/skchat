"""Deterministic inspection of a captured PNG.

This module exists because of the single most important fact about the
surface under test: Flutter web renders into a CANVAS, so
``document.body.innerText`` is EMPTY on a page that is working perfectly.
Every DOM-text assertion therefore reports a healthy app as broken, and,
worse, reports a genuinely broken app (the blank grey screen) identically to
a healthy one. The only honest inspection is the image.

So the grading step looks at PIXELS, and it does so in two layers:

  1. THIS module, which is deterministic, offline, and never needs a model.
     A blank/grey screen is a screenshot where essentially one colour covers
     the whole frame. That is a measurement, not an opinion, so it is the
     only thing allowed to raise a run to ``problem``.
  2. :mod:`skchat.browser_qa.grade`, which asks skgateway to look at the
     same image. That layer is advisory: it can add nuance and it can add
     ``notable``, but it can never manufacture a ``problem``, because a
     model having a bad morning must not file a GTD item.

The motivating bug: a shipped share-link route rendered a blank grey screen
because the route did an unguarded cast on a router extra that is null for a
shared link. The compiled bundle contained the route, the suite was green,
and it was caught only when a human finally loaded the page. Layer 1 catches
exactly that class without asking anyone's opinion.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

#: A frame where one colour covers at least this fraction of the pixels is
#: "uniform" (the blank/grey-screen signature). Deliberately very close to
#: 1.0: a real Flutter shell that has painted anything at all, even a bare
#: app bar on a flat background, sits far below this.
UNIFORM_DOMINANT_FRACTION = 0.995

#: ...or a frame with no more than this many distinct colours at all.
UNIFORM_DISTINCT_MAX = 2

#: Longest edge the frame is reduced to before counting colours. NEAREST
#: resampling on purpose: it never invents an intermediate colour, so the
#: distinct-colour count stays a real measurement of the original frame.
SAMPLE_EDGE = 400

#: Anything smaller than this is a failed capture, not a rendered page.
MIN_PIXELS = 1024

#: Longest edge of the copy handed to the model in :mod:`grade`. Keeps the
#: request body sane; a full 1280x900 PNG base64s into megabytes.
GRADING_EDGE = 1024


class ScreenshotError(ValueError):
    """The bytes handed in are not a usable image."""


@dataclass
class ScreenshotStats:
    """What the pixels say, with no interpretation layered on top."""

    width: int
    height: int
    distinct_colors: int
    dominant_fraction: float
    mean_luma: float
    luma_stddev: float

    @property
    def is_uniform(self) -> bool:
        """The blank/grey-screen signature (see module docstring)."""
        return (
            self.dominant_fraction >= UNIFORM_DOMINANT_FRACTION
            or self.distinct_colors <= UNIFORM_DISTINCT_MAX
        )

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "distinct_colors": self.distinct_colors,
            "dominant_fraction": round(self.dominant_fraction, 6),
            "mean_luma": round(self.mean_luma, 3),
            "luma_stddev": round(self.luma_stddev, 3),
            "is_uniform": self.is_uniform,
        }


def _open(data: bytes):
    from PIL import Image  # hard skchat dependency, see pyproject

    if not data:
        raise ScreenshotError("empty screenshot")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same answer
        raise ScreenshotError(f"undecodable screenshot: {exc}") from exc
    return img


def inspect_png(data: bytes) -> ScreenshotStats:
    """Measure a captured frame. Raises :class:`ScreenshotError` when the
    bytes are not an image at all, which the lane treats as a failed capture
    rather than as a verdict about the page."""
    from PIL import Image, ImageStat

    img = _open(data)
    width, height = img.size
    if width * height < MIN_PIXELS:
        raise ScreenshotError(f"screenshot too small to judge: {width}x{height}")

    rgb = img.convert("RGB")
    sample = rgb.copy()
    sample.thumbnail((SAMPLE_EDGE, SAMPLE_EDGE), Image.NEAREST)
    total = sample.size[0] * sample.size[1]
    colors = sample.getcolors(maxcolors=total + 1) or []
    distinct = len(colors)
    dominant = (max(count for count, _ in colors) / total) if colors else 1.0

    stat = ImageStat.Stat(sample.convert("L"))
    return ScreenshotStats(
        width=width,
        height=height,
        distinct_colors=distinct,
        dominant_fraction=dominant,
        mean_luma=float(stat.mean[0]),
        luma_stddev=float(stat.stddev[0]),
    )


def encode_for_grading(data: bytes, *, edge: int = GRADING_EDGE) -> bytes:
    """Return the smallest usable PNG encoding of a frame for the model pass.

    The goal is a bounded request body, so BYTES are the metric, not pixels.
    Downscaling almost always wins on a real screenshot (large flat regions
    of interface), but it is not guaranteed to: re-encoding dense
    high-entropy pixels can produce a bigger file than the original. When
    that happens the original is already the smaller payload, so it is what
    goes on the wire.

    The input is returned unchanged when it is already within ``edge`` or
    cannot be decoded at all; in the undecodable case the grader's own
    failure path then reports an honest skip rather than this function
    guessing.
    """
    from PIL import Image

    try:
        img = _open(data)
    except ScreenshotError:
        return data
    if max(img.size) <= edge:
        return data
    small = img.convert("RGB")
    small.thumbnail((edge, edge), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, format="PNG", optimize=True)
    reduced = buf.getvalue()
    return reduced if len(reduced) < len(data) else data


#: Size of the synthetic image used to prove a grader can actually see (see
#: :func:`make_vision_probe`).
VISION_PROBE_SIZE = (320, 200)


def make_vision_probe(code: str) -> bytes:
    """A white PNG with ``code`` printed large and black in the middle.

    This exists because a text-only model asked to judge a screenshot does
    not say "I cannot see it". It answers anyway. Observed in the field on
    this fleet: ``sk-default`` routed to a ``text->text`` model, which
    scored a perfectly rendered onboarding screen 1 out of 5 with the note
    "the page shows no usable UI", having judged the console log alone. That
    is a fabricated verdict, which is the one thing the grading pass must
    never produce.

    A model that can read three digits off this image can see. One that
    cannot will guess, and will almost always guess wrong, so
    :func:`skchat.browser_qa.grade.probe_vision` turns "the configured model
    is blind" into an honest skip instead of a confident lie.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", VISION_PROBE_SIZE, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=140)
    except TypeError:  # Pillow older than 10.1 has no sized default font
        font = ImageFont.load_default()
    draw.text(
        (VISION_PROBE_SIZE[0] // 2, VISION_PROBE_SIZE[1] // 2),
        code,
        fill=(0, 0, 0),
        font=font,
        anchor="mm",
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


__all__ = [
    "GRADING_EDGE",
    "VISION_PROBE_SIZE",
    "make_vision_probe",
    "MIN_PIXELS",
    "ScreenshotError",
    "ScreenshotStats",
    "UNIFORM_DISTINCT_MAX",
    "UNIFORM_DOMINANT_FRACTION",
    "encode_for_grading",
    "inspect_png",
]
