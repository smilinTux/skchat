"""The model pass: grade a SCREENSHOT, not a string.

This reuses the WD-7 grading DISCIPLINE, not its code. The four things that
transfer, and are enforced here:

  1. AN INDEPENDENT PASS. The grader sees the captured frame, the route it
     was captured from, and the console diagnostics. It sees nothing about
     how the lane drove the browser, no step log, no prior verdict, and no
     hint of what answer would be convenient.
  2. A 1-TO-5 INTEGER SCALE. Every dimension and the overall score must be
     an integer in 1..5. A float, a string, or an out-of-range value fails
     to parse, which is a skip, not a guess.
  3. A REQUIRED VERDICT TOKEN. The reply must carry a literal ``PASS`` or
     ``FAIL``. Only a reply disciplined enough to emit that exact token is
     trusted to have produced real scores at all. A chatty paragraph with a
     number in it is not a score.
  4. A DETERMINISTIC THRESHOLD. The returned verdict is recomputed in code
     from the parsed scores against :data:`THRESHOLD` / :data:`FLOOR`. The
     model's own token gates the parse; the pass/fail DECISION is always
     ours, so a model claiming PASS while scoring below threshold cannot
     smuggle one through.

NEVER FABRICATES. Any failure at all, skgateway unreachable, a timeout, an
unparseable body, a missing verdict token, an out-of-range dimension,
returns ``BrowserGrade(graded=False, skip_reason=...)``. The run then
carries a noted GAP. A missing grade is fine. An invented one is a lie in a
document Chef reads every morning.

A BLIND GRADER IS A FABRICATED VERDICT, so the model is asked to prove it
can see before it is trusted to judge a picture (:func:`probe_vision`). This
is not hypothetical. On this fleet ``sk-default`` currently routes to
``openai/gpt-oss-20b``, whose model card reads ``modality: text->text``. Sent
a screenshot of a correctly rendered onboarding screen, it did not say it
could not see the image. It scored the screen 1 out of 5 with the note "the
page shows no usable UI", having judged the console log alone. Every run
would have carried that line. The probe turns it into an honest skip.

CANNOT MANUFACTURE WORK. Even a clean ``fail`` verdict is worth at most
``notable`` in the digest. Only the deterministic pixel measurement in
:mod:`skchat.browser_qa.screenshot` can raise a run to ``problem``, because
``problem`` files a GTD item and can escalate to a staged card, and a flaky
opinion must not do that every morning. See
:func:`skchat.browser_qa.lane.compute_severity`.

Endpoint and model are env-driven and the model is NEVER a hardcoded
concrete model: ``sk-default`` is the skgateway auto-router's own alias.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

SKGATEWAY_URL = os.environ.get("SKGATEWAY_URL", "http://localhost:18780/v1")
#: Always the router alias, never a concrete model id.
SKGATEWAY_MODEL = os.environ.get("SKGATEWAY_MODEL", "sk-default")

#: Wall-clock budget for ONE grade call. A hung gateway must never make the
#: digest late, so this is short and the lane treats the timeout as a skip.
DEFAULT_TIMEOUT_S = 45.0

#: The versioned rubric identity carried on every result, including skips,
#: so a reader always knows WHICH rubric an attempt was against. The rubric
#: itself is these three dimensions plus the threshold below; it is a
#: constant here rather than a YAML file because it grades one fixed
#: artifact shape and has no per-caller variation.
RUBRIC_ID = "skchat-browser-qa"
RUBRIC_VERSION = 1
RUBRIC_REF = f"{RUBRIC_ID}@v{RUBRIC_VERSION}"

DIMENSIONS: tuple[tuple[str, str], ...] = (
    (
        "rendered",
        "Did the page actually paint a user interface? 1 means a blank, "
        "single-colour, or grey screen with no interface at all. 5 means a "
        "populated interface with real structure.",
    ),
    (
        "coherent",
        "Does what painted look like a working screen rather than a broken "
        "one? 1 means visible error text, a stack trace, an empty error "
        "frame, or obviously mangled layout. 5 means a coherent screen.",
    ),
    (
        "clean_console",
        "Do the console diagnostics look healthy? 1 means uncaught "
        "exceptions or failed resource loads. 5 means nothing alarming.",
    ),
)

THRESHOLD = 3  # overall must be at least this
FLOOR = 2  # and no single dimension may be below this

_VALID_VERDICT_TOKENS = ("PASS", "FAIL")


#: Wall-clock budget for the vision capability probe (see
#: :func:`probe_vision`). Short: it is one tiny image and three digits.
VISION_PROBE_TIMEOUT_S = 25.0

#: Set to 0 to skip the capability probe when the configured model is known
#: to be multimodal, saving one call per run. On by default: a silently
#: text-only router is the failure this exists to catch.
VERIFY_VISION = os.environ.get("SKCHAT_BROWSER_QA_VERIFY_VISION", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


class SkipReason:
    """The only ways a grade is skipped. There is never a silent third."""

    GATEWAY_UNREACHABLE = "gateway_unreachable"
    UNPARSEABLE_REPLY = "unparseable_reply"
    NO_EVIDENCE = "no_evidence"
    #: The gateway answered, but the model it routed to cannot see images.
    #: Observed in the field: `sk-default` routed to a `text->text` model
    #: which scored a perfectly rendered screen 1 out of 5 from the console
    #: log alone. A blind grader does not decline, it invents, so this is
    #: gated rather than trusted.
    VISION_UNAVAILABLE = "vision_unavailable"


@dataclass
class BrowserGrade:
    """The grade for ONE captured frame.

    ``graded=False`` means "no score exists": ``scores`` is empty, ``overall``
    is None, and ``verdict`` is the empty string. Never a guessed value.
    """

    graded: bool
    rubric_ref: str = RUBRIC_REF
    subject_ref: str = ""
    scores: dict = field(default_factory=dict)
    overall: Optional[int] = None
    verdict: str = ""
    notes: str = ""
    skip_reason: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "graded": self.graded,
            "rubric_ref": self.rubric_ref,
            "subject_ref": self.subject_ref,
            "scores": dict(self.scores),
            "overall": self.overall,
            "verdict": self.verdict,
            "notes": self.notes,
            "skip_reason": self.skip_reason,
            "model": self.model,
        }


def build_prompt(*, route: str, console_lines: list[str]) -> str:
    """Compose the grading prompt. Pure and deterministic. The grader is
    told what it is looking at and nothing about how it got there
    (discipline 1)."""
    dims = "\n".join(f'- "{key}": {prompt}' for key, prompt in DIMENSIONS)
    keys_example = ", ".join(f'"{key}": N' for key, _ in DIMENSIONS)
    console_block = "\n".join(console_lines[:25]) or "(no console diagnostics captured)"
    return "\n".join(
        [
            "You are an independent QA grader. You are shown one screenshot "
            "of a web application and its browser console diagnostics. Judge "
            "only what you can see.",
            "",
            f"Route captured: {route}",
            "",
            "This application renders into a canvas, so the absence of "
            "selectable text is normal and is NOT by itself a fault. Judge "
            "the picture.",
            "",
            f"Console diagnostics:\n{console_block}",
            "",
            "Score each dimension 1 to 5, integers only:",
            dims,
            "",
            "Reply with STRICT JSON ONLY, no prose outside the JSON object "
            "and no markdown code fences, using exactly this shape: "
            f'{{"scores": {{{keys_example}}}, "overall": N, "verdict": "PASS" '
            'or "FAIL", "notes": "one short sentence in your own words"}. '
            "The verdict field must be exactly the literal string PASS or "
            "FAIL, uppercase, nothing else added. Never use em dashes or en "
            "dashes anywhere in notes; use commas or periods instead.",
        ]
    )


def _post_json(url: str, payload: dict, *, timeout: float) -> Optional[dict]:
    """One JSON POST. Returns the decoded body, or None on ANY failure.
    Never raises.

    Deliberately urllib rather than the ``curl`` subprocess the WD-7 grader
    uses: this payload embeds a base64 image and would otherwise be a
    multi-megabyte argv, which is a different failure mode entirely.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local gateway
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def _chat_vision(
    prompt: str, png: bytes, *, timeout: float, url: str, model: str
) -> Optional[str]:
    """Send prompt + image to skgateway. Returns the reply text, or None on
    any failure. Never raises."""
    b64 = base64.b64encode(png).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 400,
        "temperature": 0.0,
    }
    data = _post_json(f"{url}/chat/completions", payload, timeout=timeout)
    if not isinstance(data, dict):
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, list):  # some backends echo the content-part shape back
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    content = re.sub(r"<think>.*?</think>", "", str(content), flags=re.S).strip()
    return content or None


def _extract_json_object(text: str) -> Optional[dict]:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_reply(text: str) -> Optional[dict]:
    """Strict parse (disciplines 2 and 3). Every dimension present as an
    in-range integer, an in-range integer overall, and the literal verdict
    token. Anything else returns None, which the caller treats exactly like
    a down gateway: skip, never guess."""
    data = _extract_json_object(text)
    if data is None:
        return None

    raw_scores = data.get("scores")
    if not isinstance(raw_scores, dict):
        return None
    scores: dict[str, int] = {}
    for key, _ in DIMENSIONS:
        value = raw_scores.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 5):
            return None
        scores[key] = value

    overall = data.get("overall")
    if isinstance(overall, bool) or not isinstance(overall, int) or not (1 <= overall <= 5):
        return None

    if data.get("verdict") not in _VALID_VERDICT_TOKENS:
        return None

    return {
        "scores": scores,
        "overall": overall,
        "notes": _strip_dashes(str(data.get("notes") or "")[:280]),
    }


def _strip_dashes(text: str) -> str:
    """Em and en dashes are banned in every artifact this fleet writes, and
    a model's free-text note is the one place they sneak back in."""
    return text.replace("—", ", ").replace("–", "-")


def _new_probe_code() -> str:
    """A fresh three digit code per probe. Fresh on purpose: a fixed code
    could be learned, cached, or guessed from an earlier transcript."""
    return str(random.SystemRandom().randrange(100, 1000))


def probe_vision(
    *, timeout: float = VISION_PROBE_TIMEOUT_S, url: str = "", model: str = ""
) -> tuple[str, str]:
    """Prove the configured model can actually see, before trusting it to
    judge a picture.

    Returns ``("ok" | "unreachable" | "blind", detail)``.

    The distinction matters downstream: ``unreachable`` is the gateway being
    down, which is an ordinary gap. ``blind`` is worse and quieter, the
    gateway answering confidently about an image it never received. On this
    fleet `sk-default` currently routes to `openai/gpt-oss-20b`, whose card
    reads ``modality: text->text``; asked to grade a screenshot it scored a
    correctly rendered onboarding screen 1 out of 5 from the console log
    alone. A blind grader never declines, so it has to be caught, not
    trusted.

    The probe is a white image with three random digits on it. Reading them
    is trivial with vision and a one in nine hundred guess without.
    """
    url = url or SKGATEWAY_URL
    model = model or SKGATEWAY_MODEL
    code = _new_probe_code()

    from .screenshot import make_vision_probe

    reply = _chat_vision(
        "This image contains a three digit number. Reply with ONLY those three "
        "digits and nothing else. If you cannot see an image, reply with the "
        "word NONE.",
        make_vision_probe(code),
        timeout=timeout,
        url=url,
        model=model,
    )
    if not reply:
        return "unreachable", "the gateway did not answer the vision probe"
    digits = "".join(re.findall(r"\d", reply))
    if code in digits:
        return "ok", ""
    return "blind", (
        f"the configured grader ({model}) could not read a three digit test "
        "image, so it cannot see a screenshot either"
    )


def grade_screenshot(
    png: bytes,
    *,
    route: str,
    console_lines: Optional[list[str]] = None,
    subject_ref: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = "",
    model: str = "",
    verify_vision: Optional[bool] = None,
) -> BrowserGrade:
    """Grade one captured frame. Never raises, never fabricates."""
    url = url or SKGATEWAY_URL
    model = model or SKGATEWAY_MODEL
    subject_ref = subject_ref or route
    verify_vision = VERIFY_VISION if verify_vision is None else verify_vision

    if not png:
        return BrowserGrade(
            graded=False, subject_ref=subject_ref, skip_reason=SkipReason.NO_EVIDENCE, model=model
        )

    if verify_vision:
        status, detail = probe_vision(url=url, model=model)
        if status == "unreachable":
            return BrowserGrade(
                graded=False,
                subject_ref=subject_ref,
                skip_reason=SkipReason.GATEWAY_UNREACHABLE,
                model=model,
            )
        if status == "blind":
            return BrowserGrade(
                graded=False,
                subject_ref=subject_ref,
                skip_reason=SkipReason.VISION_UNAVAILABLE,
                notes=detail,
                model=model,
            )

    from .screenshot import encode_for_grading

    prompt = build_prompt(route=route, console_lines=list(console_lines or []))
    text = _chat_vision(prompt, encode_for_grading(png), timeout=timeout, url=url, model=model)
    if not text:
        return BrowserGrade(
            graded=False,
            subject_ref=subject_ref,
            skip_reason=SkipReason.GATEWAY_UNREACHABLE,
            model=model,
        )

    parsed = parse_reply(text)
    if parsed is None:
        return BrowserGrade(
            graded=False,
            subject_ref=subject_ref,
            skip_reason=SkipReason.UNPARSEABLE_REPLY,
            model=model,
        )

    # Discipline 4: the decision is recomputed here, never taken from the
    # model's own token.
    passes = parsed["overall"] >= THRESHOLD and min(parsed["scores"].values()) >= FLOOR
    return BrowserGrade(
        graded=True,
        subject_ref=subject_ref,
        scores=parsed["scores"],
        overall=parsed["overall"],
        verdict="pass" if passes else "fail",
        notes=parsed["notes"],
        model=model,
    )


__all__ = [
    "BrowserGrade",
    "DIMENSIONS",
    "FLOOR",
    "RUBRIC_REF",
    "SKGATEWAY_MODEL",
    "SKGATEWAY_URL",
    "SkipReason",
    "THRESHOLD",
    "VERIFY_VISION",
    "build_prompt",
    "grade_screenshot",
    "parse_reply",
    "probe_vision",
]
