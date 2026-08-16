"""Tests for the WD-10 grading pass and pixel inspection.

NO TEST HERE CALLS A MODEL OR OPENS A SOCKET. The one HTTP boundary
(``_post_json``) is monkeypatched, and the autouse ``sealed`` fixture makes
it raise by default so a forgotten patch fails loudly rather than posting a
screenshot to whatever is listening on 18780.

The four disciplines under test, inherited from WD-7: an independent pass, a
1-to-5 integer scale, a required verdict token, and a threshold recomputed in
code. Plus the rule that matters most downstream: this layer NEVER fabricates
a verdict, and can never on its own raise a run to ``problem``.
"""

from __future__ import annotations

import io
import socket

import pytest

from skchat.browser_qa import grade as grade_mod
from skchat.browser_qa import screenshot as shot_mod
from skchat.browser_qa.grade import BrowserGrade, SkipReason, grade_screenshot, parse_reply


@pytest.fixture(autouse=True)
def sealed(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a test tried to call the real gateway or open a socket")

    monkeypatch.setattr(grade_mod, "_post_json", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _png(color=(20, 120, 220), size=(40, 30), noisy=False) -> bytes:
    from PIL import Image

    img = Image.new("RGB", size, color)
    if noisy:
        for x in range(size[0]):
            for y in range(size[1]):
                img.putpixel((x, y), ((x * 5) % 256, (y * 9) % 256, (x * y) % 256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _reply(content):
    return {"choices": [{"message": {"content": content}}]}


def _serve(monkeypatch, content, capture=None):
    """Serve one canned reply. The vision probe is satisfied separately so
    the tests below exercise grading, not the capability gate."""

    def fake_post(url, payload, *, timeout):
        if capture is not None:
            capture["url"] = url
            capture["payload"] = payload
            capture["timeout"] = timeout
        return _reply(content)

    monkeypatch.setattr(grade_mod, "_post_json", fake_post)
    monkeypatch.setattr(grade_mod, "probe_vision", lambda **_kw: ("ok", ""))


GOOD = '{"scores": {"rendered": 5, "coherent": 4, "clean_console": 4}, "overall": 4, "verdict": "PASS", "notes": "Fine."}'


# ------------------------------------------------------------- the happy path


def test_a_clean_reply_grades(monkeypatch):
    _serve(monkeypatch, GOOD)
    result = grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert result.graded and result.verdict == "pass" and result.overall == 4
    assert result.scores == {"rendered": 5, "coherent": 4, "clean_console": 4}
    assert result.rubric_ref == grade_mod.RUBRIC_REF


def test_the_image_is_actually_sent(monkeypatch):
    """The whole point: this app paints into a canvas, so the grader must
    look at an IMAGE, not at a string."""
    captured = {}
    _serve(monkeypatch, GOOD, captured)
    grade_screenshot(_png(noisy=True), route="http://h/app/")
    content = captured["payload"]["messages"][0]["content"]
    kinds = [part["type"] for part in content]
    assert "image_url" in kinds
    image = next(p for p in content if p["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_the_model_is_the_router_alias_never_a_concrete_model(monkeypatch):
    captured = {}
    _serve(monkeypatch, GOOD, captured)
    grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert captured["payload"]["model"] == "sk-default"
    assert captured["url"].endswith("/chat/completions")


def test_the_prompt_never_reveals_how_the_lane_drove_the_browser():
    """Discipline 1, the independent pass: the grader sees the route, the
    picture and the console, and nothing about the walk that produced them."""
    prompt = grade_mod.build_prompt(route="http://h/app/", console_lines=["[log] hi"])
    lowered = prompt.lower()
    for leak in ("step", "settle", "cdp", "playwright", "expected", "should pass"):
        assert leak not in lowered


def test_the_prompt_warns_that_empty_text_is_normal():
    prompt = grade_mod.build_prompt(route="http://h/app/", console_lines=[])
    assert "canvas" in prompt.lower()


# ---------------------------------------------------- never fabricates a score


def test_an_unreachable_gateway_is_a_skip_not_a_guess(monkeypatch):
    monkeypatch.setattr(grade_mod, "_post_json", lambda *a, **k: None)
    result = grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert not result.graded
    assert result.skip_reason == SkipReason.GATEWAY_UNREACHABLE
    assert result.overall is None and result.scores == {} and result.verdict == ""
    assert result.rubric_ref == grade_mod.RUBRIC_REF  # still says WHICH rubric


def test_no_evidence_is_a_skip():
    result = grade_screenshot(b"", route="http://h/app/")
    assert not result.graded and result.skip_reason == SkipReason.NO_EVIDENCE


# ------------------------------------------------- the vision capability gate


def test_a_blind_grader_is_skipped_not_believed(monkeypatch):
    """A text-only model asked to judge a screenshot does not decline, it
    invents. Observed in the field: `sk-default` routed to a `text->text`
    model and scored a correctly rendered onboarding screen 1 out of 5 from
    the console log alone. That is a fabricated verdict."""
    monkeypatch.setattr(grade_mod, "probe_vision", lambda **_kw: ("blind", "cannot see"))

    def must_not_be_called(*_a, **_k):
        raise AssertionError("a blind grader must never be asked for a verdict")

    monkeypatch.setattr(grade_mod, "_post_json", must_not_be_called)
    result = grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert not result.graded
    assert result.skip_reason == SkipReason.VISION_UNAVAILABLE
    assert result.overall is None and result.verdict == ""
    assert "cannot see" in result.notes


def test_a_probe_the_gateway_never_answers_is_an_ordinary_outage(monkeypatch):
    monkeypatch.setattr(grade_mod, "probe_vision", lambda **_kw: ("unreachable", "down"))
    result = grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert result.skip_reason == SkipReason.GATEWAY_UNREACHABLE


def test_the_probe_reads_digits_off_a_real_generated_image(monkeypatch):
    """The probe image is generated here and the reply is checked against
    the code that was drawn, so a lucky guess cannot pass."""
    seen = {}

    def fake_chat(prompt, png, *, timeout, url, model):
        seen["png"] = png
        # Decode the code the way a model with eyes would: it is the only
        # dark ink on a white field, so simply assert a real image arrived
        # and answer with the code the module chose.
        return seen["code"]

    monkeypatch.setattr(grade_mod, "_new_probe_code", lambda: seen.setdefault("code", "417"))
    monkeypatch.setattr(grade_mod, "_chat_vision", fake_chat)
    assert grade_mod.probe_vision()[0] == "ok"
    assert seen["png"].startswith(b"\x89PNG")


def test_the_probe_rejects_a_wrong_answer(monkeypatch):
    monkeypatch.setattr(grade_mod, "_new_probe_code", lambda: "417")
    monkeypatch.setattr(grade_mod, "_chat_vision", lambda *a, **k: "I think it says 902.")
    status, detail = grade_mod.probe_vision()
    assert status == "blind" and "could not read" in detail


def test_the_probe_rejects_a_model_that_admits_nothing_is_there(monkeypatch):
    monkeypatch.setattr(grade_mod, "_new_probe_code", lambda: "417")
    monkeypatch.setattr(grade_mod, "_chat_vision", lambda *a, **k: "NONE")
    assert grade_mod.probe_vision()[0] == "blind"


def test_a_silent_gateway_is_unreachable_not_blind(monkeypatch):
    monkeypatch.setattr(grade_mod, "_chat_vision", lambda *a, **k: None)
    assert grade_mod.probe_vision()[0] == "unreachable"


def test_probe_codes_are_fresh_each_time():
    codes = {grade_mod._new_probe_code() for _ in range(60)}
    assert len(codes) > 1, "a fixed probe code could be learned or cached"
    assert all(len(c) == 3 and c.isdigit() for c in codes)


def test_the_probe_can_be_turned_off_for_a_known_multimodal_gateway(monkeypatch):
    monkeypatch.setattr(
        grade_mod,
        "probe_vision",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("the probe should be skipped")),
    )
    monkeypatch.setattr(grade_mod, "_post_json", lambda *a, **k: _reply(GOOD))
    assert grade_screenshot(_png(noisy=True), route="http://h/app/", verify_vision=False).graded


def test_a_generated_probe_image_actually_contains_ink():
    from skchat.browser_qa.screenshot import inspect_png, make_vision_probe

    stats = inspect_png(make_vision_probe("417"))
    assert not stats.is_uniform, "the probe must actually draw something"
    assert stats.width == shot_mod.VISION_PROBE_SIZE[0]


@pytest.mark.parametrize(
    "content",
    [
        "The page looks broadly fine to me, I would say a four out of five.",
        '{"scores": {"rendered": 5}, "overall": 5, "verdict": "PASS"}',  # missing dimensions
        '{"scores": {"rendered": 5, "coherent": 4, "clean_console": 4}, "overall": 4}',  # no token
        '{"scores": {"rendered": 5, "coherent": 4, "clean_console": 4}, "overall": 4, "verdict": "probably fine"}',
        '{"scores": {"rendered": 4.5, "coherent": 4, "clean_console": 4}, "overall": 4, "verdict": "PASS"}',
        '{"scores": {"rendered": 9, "coherent": 4, "clean_console": 4}, "overall": 4, "verdict": "PASS"}',
        '{"scores": {"rendered": true, "coherent": 4, "clean_console": 4}, "overall": 4, "verdict": "PASS"}',
        '{"scores": {"rendered": 5, "coherent": 4, "clean_console": 4}, "overall": 0, "verdict": "PASS"}',
        "",
    ],
)
def test_an_undisciplined_reply_is_skipped_never_salvaged(monkeypatch, content):
    _serve(monkeypatch, content)
    result = grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert not result.graded
    assert result.skip_reason in (SkipReason.UNPARSEABLE_REPLY, SkipReason.GATEWAY_UNREACHABLE)
    assert result.overall is None


def test_json_wrapped_in_prose_is_still_read(monkeypatch):
    _serve(monkeypatch, f"Here you go:\n```json\n{GOOD}\n```\nHope that helps.")
    assert grade_screenshot(_png(noisy=True), route="http://h/app/").graded


def test_a_think_block_is_stripped(monkeypatch):
    _serve(monkeypatch, f"<think>hmm, the shell looks painted</think>{GOOD}")
    assert grade_screenshot(_png(noisy=True), route="http://h/app/").graded


# ----------------------------------------------- the decision is always ours


def test_a_claimed_PASS_below_threshold_cannot_smuggle_a_pass(monkeypatch):
    """Discipline 4: the verdict is recomputed from the parsed scores, never
    taken from the model's own token."""
    _serve(
        monkeypatch,
        '{"scores": {"rendered": 1, "coherent": 1, "clean_console": 1}, '
        '"overall": 1, "verdict": "PASS", "notes": "trust me"}',
    )
    result = grade_screenshot(_png(noisy=True), route="http://h/app/")
    assert result.graded and result.verdict == "fail"


def test_a_claimed_FAIL_above_threshold_still_passes(monkeypatch):
    _serve(
        monkeypatch,
        '{"scores": {"rendered": 5, "coherent": 5, "clean_console": 5}, '
        '"overall": 5, "verdict": "FAIL", "notes": "nope"}',
    )
    assert grade_screenshot(_png(noisy=True), route="http://h/app/").verdict == "pass"


def test_one_dimension_below_the_floor_fails_the_whole_grade(monkeypatch):
    _serve(
        monkeypatch,
        '{"scores": {"rendered": 1, "coherent": 5, "clean_console": 5}, '
        '"overall": 4, "verdict": "PASS", "notes": "mostly"}',
    )
    assert grade_screenshot(_png(noisy=True), route="http://h/app/").verdict == "fail"


def test_dashes_are_stripped_from_the_model_note(monkeypatch):
    _serve(
        monkeypatch,
        '{"scores": {"rendered": 5, "coherent": 5, "clean_console": 5}, "overall": 5, '
        '"verdict": "PASS", "notes": "the shell painted \\u2014 nothing alarming \\u2013 fine"}',
    )
    notes = grade_screenshot(_png(noisy=True), route="http://h/app/").notes
    assert "—" not in notes and "–" not in notes


def test_a_content_part_list_reply_is_still_read(monkeypatch):
    def fake_post(url, payload, *, timeout):
        return {"choices": [{"message": {"content": [{"type": "text", "text": GOOD}]}}]}

    monkeypatch.setattr(grade_mod, "_post_json", fake_post)
    monkeypatch.setattr(grade_mod, "probe_vision", lambda **_kw: ("ok", ""))
    assert grade_screenshot(_png(noisy=True), route="http://h/app/").graded


def test_parse_reply_is_pure():
    assert parse_reply(GOOD)["overall"] == 4
    assert parse_reply("garbage") is None


def test_the_grade_serializes_flat():
    doc = BrowserGrade(graded=False, skip_reason="x").to_dict()
    assert set(doc) == {
        "graded",
        "rubric_ref",
        "subject_ref",
        "scores",
        "overall",
        "verdict",
        "notes",
        "skip_reason",
        "model",
    }


# ------------------------------------------------------- pixel inspection ---


def test_a_flat_grey_frame_reads_as_uniform():
    """The blank grey screen the share-link bug shipped: one colour, whole
    frame, no opinion required."""
    stats = shot_mod.inspect_png(_png(color=(128, 128, 128)))
    assert stats.is_uniform and stats.distinct_colors == 1
    assert stats.dominant_fraction == pytest.approx(1.0)


def test_a_painted_frame_does_not_read_as_uniform():
    stats = shot_mod.inspect_png(_png(noisy=True))
    assert not stats.is_uniform and stats.distinct_colors > 2


def test_a_nearly_flat_frame_with_one_stray_pixel_still_reads_as_uniform():
    from PIL import Image

    img = Image.new("RGB", (200, 200), (240, 240, 240))
    img.putpixel((0, 0), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert shot_mod.inspect_png(buf.getvalue()).is_uniform


def test_undecodable_bytes_raise_rather_than_reading_as_blank():
    """A failed capture is a failed capture. It must never be silently
    reported as a blank page, which would be a different bug entirely."""
    with pytest.raises(shot_mod.ScreenshotError):
        shot_mod.inspect_png(b"not a png at all")
    with pytest.raises(shot_mod.ScreenshotError):
        shot_mod.inspect_png(b"")


def test_a_one_pixel_capture_is_rejected():
    with pytest.raises(shot_mod.ScreenshotError):
        shot_mod.inspect_png(_png(size=(2, 2)))


def test_a_large_realistic_frame_is_downscaled_before_grading():
    """A real screenshot is mostly flat interface, so downscaling wins on
    both dimensions and bytes."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (2000, 1400), (250, 250, 252))
    draw = ImageDraw.Draw(img)
    for i in range(0, 1400, 90):
        draw.rectangle([60, i + 10, 1900, i + 60], fill=(200 - i % 60, 210, 235))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    big = buf.getvalue()

    small = shot_mod.encode_for_grading(big)
    assert len(small) < len(big)
    assert max(Image.open(io.BytesIO(small)).size) <= shot_mod.GRADING_EDGE


def test_the_grading_payload_is_never_grown_by_re_encoding():
    """Dense high-entropy pixels can re-encode LARGER than the original. The
    metric that matters is the request body, so the smaller one wins."""
    big = _png(size=(2000, 1400), noisy=True)
    assert len(shot_mod.encode_for_grading(big)) <= len(big)


def test_a_small_frame_is_passed_through_unchanged():
    tiny = _png(noisy=True)
    assert shot_mod.encode_for_grading(tiny) is tiny


def test_stats_serialize_for_the_artifact():
    doc = shot_mod.inspect_png(_png(noisy=True)).to_dict()
    assert doc["is_uniform"] is False and doc["width"] == 40
