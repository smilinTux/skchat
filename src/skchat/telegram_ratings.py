"""Telegram answer-rating store for the sk-auto difficulty router.

This is the DATA half of coord task c87faa13: an append-only JSONL of "the bot
answered message X with model M" rows plus later "the human rated X with N stars"
rows. The skgateway sk-auto router reads the same file (Node side) and NUDGES its
heuristic difficulty routing toward models that empirically score well for a given
prompt class. The Telegram UI (👍/👎 buttons + callback) that CALLS this store
lives in ``scripts/telegram_bridge.py`` / ``scripts/bridge_consciousness.py``
(owned by a separate agent) — this module is only the persistence port.

Shared interface contract (Python + Node code to this EXACTLY)
--------------------------------------------------------------
- **Path:** ``~/.skcapstone/models/ratings.jsonl`` (next to the model
  ``registry.yaml``). Overridable via env ``SKMODELS_RATINGS`` (Python) /
  ``SK_RATINGS_PATH`` (Node — same file, different env name by design).
- **One JSON object per line**, schema::

      {"ts": <float epoch>, "chat_id": <str>, "msg_id": <str>,
       "model": <str|null>, "prompt_class": <str|null>,
       "prompt_hash": <str|null>, "score": <int 1..5 | null>}

- **Write model (append-only, last-write-wins per (chat_id, msg_id)):**
    * When the bot answers, call :func:`record_send` — appends a row with
      ``score=null`` capturing which ``model`` served which ``msg_id`` (plus an
      optional ``prompt_class`` / ``prompt_hash``).
    * When the human rates, call :func:`record_rating` — appends a NEW row with
      the SAME ``(chat_id, msg_id)`` and ``score`` set. It back-fills
      ``model`` / ``prompt_class`` / ``prompt_hash`` from the prior send row so
      each rating row is self-contained (aggregation never needs a join).
  Aggregators collapse rows by ``(chat_id, msg_id)`` taking the LAST write.

Design mirrors ``rating.py`` (RenderRecord/record_score) and
``rating_reactions.py`` (JSONL sidecar + thread lock). Thumbs → score mapping
(👍→5 / 👎→1) is done at the CALL site (the bridge); this store takes an int.

stdlib-only (json) — no third-party deps.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Score range. 1 = 👎 (bad answer), 5 = 👍 (great answer).
SCORE_MIN, SCORE_MAX = 1, 5

# Default aggregation window (most-recent rated messages considered).
DEFAULT_WINDOW = 500

_lock = threading.Lock()


def ratings_path() -> Path:
    """Absolute path to the ratings JSONL, honouring ``SKMODELS_RATINGS``.

    Defaults to ``~/.skcapstone/models/ratings.jsonl`` (next to registry.yaml).
    """
    env = os.environ.get("SKMODELS_RATINGS")
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~/.skcapstone/models/ratings.jsonl"))


def _append_row(row: dict[str, Any]) -> None:
    path = ratings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:  # never let a rating write break the caller
        logger.warning("telegram_ratings: append failed (%s: %s)", type(exc).__name__, exc)


def _read_rows() -> list[dict[str, Any]]:
    """Read all rows (oldest→newest). Missing file → []. Bad lines skipped."""
    path = ratings_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception as exc:
        logger.warning("telegram_ratings: read failed (%s: %s)", type(exc).__name__, exc)
        return []
    return rows


def _latest_send(chat_id: str, msg_id: str) -> Optional[dict[str, Any]]:
    """Newest row for this (chat_id, msg_id), used to back-fill a rating row."""
    key = (str(chat_id), str(msg_id))
    found: Optional[dict[str, Any]] = None
    for row in _read_rows():
        if (str(row.get("chat_id")), str(row.get("msg_id"))) == key:
            found = row  # keep iterating → last one wins
    return found


def record_send(
    chat_id: str,
    msg_id: str,
    model: str,
    prompt_hash: str | None = None,
    prompt_class: str | None = None,
) -> None:
    """Append a "bot answered" row (``score=null``). Call once per bot answer."""
    _append_row(
        {
            "ts": time.time(),
            "chat_id": str(chat_id),
            "msg_id": str(msg_id),
            "model": str(model) if model is not None else None,
            "prompt_class": prompt_class,
            "prompt_hash": prompt_hash,
            "score": None,
        }
    )
    logger.info("telegram_ratings.record_send chat=%s msg=%s model=%s", chat_id, msg_id, model)


def record_rating(
    chat_id: str,
    msg_id: str,
    score: int,
    note: str | None = None,
) -> dict | None:
    """Append a rating row (``score`` set) for a previously-sent message.

    Back-fills ``model`` / ``prompt_class`` / ``prompt_hash`` from the prior send
    row so the rating row is self-contained. Returns the appended row, or None if
    ``score`` is out of range. Map 👍→5 / 👎→1 at the call site (the bridge).
    """
    score = int(score)
    if not (SCORE_MIN <= score <= SCORE_MAX):
        logger.warning("telegram_ratings.record_rating: score %s out of 1..5", score)
        return None

    send = _latest_send(chat_id, msg_id) or {}
    row: dict[str, Any] = {
        "ts": time.time(),
        "chat_id": str(chat_id),
        "msg_id": str(msg_id),
        "model": send.get("model"),
        "prompt_class": send.get("prompt_class"),
        "prompt_hash": send.get("prompt_hash"),
        "score": score,
    }
    if note is not None:
        row["note"] = note
    _append_row(row)
    logger.info(
        "telegram_ratings.record_rating chat=%s msg=%s score=%d model=%s",
        chat_id,
        msg_id,
        score,
        row.get("model"),
    )
    return row


def aggregate(
    prompt_class: str | None = None,
    model: str | None = None,
    window: int = DEFAULT_WINDOW,
) -> dict:
    """Aggregate recent RATED rows into per-(model, prompt_class) stats.

    Rows are collapsed by ``(chat_id, msg_id)`` (last write wins), rated rows are
    ordered by timestamp, the most-recent ``window`` are kept, then grouped.

    :returns: ``{(model, prompt_class): {"n": int, "mean": float}}``. When
        ``prompt_class`` / ``model`` are given, only matching buckets are returned.
    """
    # Collapse by (chat_id, msg_id) → last write.
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _read_rows():
        key = (str(row.get("chat_id")), str(row.get("msg_id")))
        merged[key] = row

    # Keep rated rows that carry a model, ordered by ts (most recent last).
    rated = [
        r
        for r in merged.values()
        if r.get("score") is not None and r.get("model") is not None
    ]
    rated.sort(key=lambda r: r.get("ts") or 0.0)
    if window and window > 0:
        rated = rated[-window:]

    buckets: dict[tuple[str, str | None], list[int]] = {}
    for r in rated:
        m = r.get("model")
        pc = r.get("prompt_class")
        if model is not None and m != model:
            continue
        if prompt_class is not None and pc != prompt_class:
            continue
        buckets.setdefault((m, pc), []).append(int(r["score"]))

    out: dict[tuple[str, str | None], dict[str, Any]] = {}
    for key, scores in buckets.items():
        out[key] = {"n": len(scores), "mean": round(sum(scores) / len(scores), 4)}
    return out
