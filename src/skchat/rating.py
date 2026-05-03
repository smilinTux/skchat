"""Rating + feedback-loop sidecar for ComfyUI renders.

Per-image sidecar JSON in ``~/clawd/comfyui-shared/ratings/<image_id>.json``
captures what was generated; ratings are added later via reaction handler or CLI.
A rollup of LoRA / checkpoint / beat scores feeds back into gen-time selection.

Pure metadata layer — no opinion on what the images contain. Works for any
ComfyUI render (worship, daily-look, AI LIFE, etc.).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

RATINGS_DIR = Path(
    os.environ.get(
        "SKCHAT_RATINGS_DIR",
        os.path.expanduser("~/clawd/comfyui-shared/ratings"),
    )
)
ROLLUP_PATH = Path(
    os.environ.get(
        "SKCHAT_RATING_ROLLUP",
        os.path.expanduser(
            "~/.skcapstone/agents/lumina/memory/long-term/render_scores.json"
        ),
    )
)

# Min number of ratings before a LoRA/checkpoint/beat is given non-default weight.
MIN_SAMPLES_FOR_WEIGHT = 3
# Score range. 1=hate, 3=neutral, 5=love. NEUTRAL is the no-tilt baseline.
SCORE_MIN, SCORE_NEUTRAL, SCORE_MAX = 1, 3, 5
# Soft-tilt bounds: top performers up to 2x, bottom performers down to 0.3x.
WEIGHT_MIN, WEIGHT_MAX = 0.3, 2.0


@dataclass
class RenderRecord:
    image_id: str
    image_path: str
    created_at: float
    prompt: str
    loras: list[list[Any]]  # [[name, model_w, clip_w], ...]
    checkpoint: str | None = None
    beat: str | None = None  # soft / warm / explicit / peak / afterglow
    seed: int | None = None
    age_anchors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    score: int | None = None
    note: str | None = None
    rated_at: float | None = None
    rated_via: str | None = None  # "telegram" | "cli" | "auto"

    def sidecar_path(self) -> Path:
        return RATINGS_DIR / f"{self.image_id}.json"

    def save(self) -> Path:
        RATINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = self.sidecar_path()
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))
        return path


def _new_image_id() -> str:
    return uuid.uuid4().hex[:12]


def record_render(
    *,
    image_path: str | Path,
    prompt: str,
    loras: list[tuple[str, float, float]] | list[list[Any]],
    checkpoint: str | None = None,
    beat: str | None = None,
    seed: int | None = None,
    age_anchors: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    image_id: str | None = None,
) -> RenderRecord:
    """Write a sidecar capturing what was generated. Call once per saved image."""
    record = RenderRecord(
        image_id=image_id or _new_image_id(),
        image_path=str(image_path),
        created_at=time.time(),
        prompt=prompt,
        loras=[list(l) for l in loras],
        checkpoint=checkpoint,
        beat=beat,
        seed=seed,
        age_anchors=list(age_anchors or []),
        extra=dict(extra or {}),
    )
    record.save()
    logger.info("rating.record_render id=%s beat=%s", record.image_id, beat)
    return record


def record_score(
    image_id: str, score: int, note: str | None = None, via: str = "cli"
) -> RenderRecord | None:
    """Add or update a rating on an existing render."""
    if not (SCORE_MIN <= score <= SCORE_MAX):
        raise ValueError(f"score must be {SCORE_MIN}..{SCORE_MAX}, got {score}")
    path = RATINGS_DIR / f"{image_id}.json"
    if not path.exists():
        # Try uuid prefix match (telegram reactions use full UUIDs).
        candidates = list(RATINGS_DIR.glob(f"{image_id}*.json"))
        if len(candidates) == 1:
            path = candidates[0]
        else:
            logger.warning("rating.record_score: no record for %s", image_id)
            return None
    data = json.loads(path.read_text())
    data["score"] = int(score)
    data["note"] = note
    data["rated_at"] = time.time()
    data["rated_via"] = via
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    logger.info("rating.record_score id=%s score=%d via=%s", image_id, score, via)
    return RenderRecord(**data)


def iter_rated() -> Iterable[dict[str, Any]]:
    if not RATINGS_DIR.exists():
        return
    for p in RATINGS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if data.get("score") is not None:
            yield data


def _bayesian_mean(scores: list[int], prior_mean: float = SCORE_NEUTRAL,
                   prior_weight: float = 3.0) -> float:
    """Shrink small samples toward neutral. Stops 1 lucky 5 from doubling weight."""
    if not scores:
        return prior_mean
    n = len(scores)
    return (prior_weight * prior_mean + sum(scores)) / (prior_weight + n)


def _score_to_weight(mean_score: float) -> float:
    """Map mean score (1..5) to multiplicative weight (0.3..2.0).

    3.0 → 1.0 (no tilt), 5.0 → 2.0 (max boost), 1.0 → 0.3 (soft demote).
    """
    if mean_score >= SCORE_NEUTRAL:
        # 3..5 → 1.0..2.0
        t = (mean_score - SCORE_NEUTRAL) / (SCORE_MAX - SCORE_NEUTRAL)
        return 1.0 + t * (WEIGHT_MAX - 1.0)
    # 1..3 → 0.3..1.0
    t = (mean_score - SCORE_MIN) / (SCORE_NEUTRAL - SCORE_MIN)
    return WEIGHT_MIN + t * (1.0 - WEIGHT_MIN)


def compute_rollup() -> dict[str, Any]:
    """Aggregate per-LoRA, per-checkpoint, per-beat scores. Pure read."""
    by_lora: dict[str, list[int]] = {}
    by_ckpt: dict[str, list[int]] = {}
    by_beat: dict[str, list[int]] = {}

    total = 0
    for rec in iter_rated():
        score = int(rec["score"])
        total += 1
        for lora in rec.get("loras") or []:
            if not lora:
                continue
            name = lora[0] if isinstance(lora, list) else lora
            by_lora.setdefault(name, []).append(score)
        if rec.get("checkpoint"):
            by_ckpt.setdefault(rec["checkpoint"], []).append(score)
        if rec.get("beat"):
            by_beat.setdefault(rec["beat"], []).append(score)

    def _summarize(buckets: dict[str, list[int]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, scores in buckets.items():
            mean = _bayesian_mean(scores)
            weight = _score_to_weight(mean) if len(scores) >= MIN_SAMPLES_FOR_WEIGHT else 1.0
            out[name] = {
                "n": len(scores),
                "mean": round(mean, 3),
                "weight": round(weight, 3),
                "raw_mean": round(sum(scores) / len(scores), 3),
            }
        return out

    return {
        "computed_at": time.time(),
        "total_rated": total,
        "loras": _summarize(by_lora),
        "checkpoints": _summarize(by_ckpt),
        "beats": _summarize(by_beat),
    }


def write_rollup() -> Path:
    rollup = compute_rollup()
    ROLLUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROLLUP_PATH.write_text(json.dumps(rollup, indent=2, sort_keys=True))
    return ROLLUP_PATH


def load_rollup() -> dict[str, Any]:
    if not ROLLUP_PATH.exists():
        return {"loras": {}, "checkpoints": {}, "beats": {}}
    try:
        return json.loads(ROLLUP_PATH.read_text())
    except Exception:
        return {"loras": {}, "checkpoints": {}, "beats": {}}


def lora_weight(name: str, rollup: dict[str, Any] | None = None) -> float:
    rollup = rollup or load_rollup()
    return float(rollup.get("loras", {}).get(name, {}).get("weight", 1.0))


def checkpoint_weight(name: str, rollup: dict[str, Any] | None = None) -> float:
    rollup = rollup or load_rollup()
    return float(rollup.get("checkpoints", {}).get(name, {}).get("weight", 1.0))


def weighted_choice(
    options: list[Any],
    keyfn,
    rng,
    rollup: dict[str, Any] | None = None,
) -> Any:
    """Pick from options biased by rated weight. ``keyfn(option)`` → name string.

    Empty/blank options keep weight 1.0 (neutral). Falls back to uniform if all zero.
    """
    if not options:
        raise ValueError("weighted_choice: empty options")
    rollup = rollup or load_rollup()
    weights = []
    for opt in options:
        try:
            name = keyfn(opt)
        except Exception:
            name = None
        weights.append(lora_weight(name, rollup) if name else 1.0)
    if sum(weights) <= 0:
        return rng.choice(options)
    return rng.choices(options, weights=weights, k=1)[0]
