"""Telegram reaction → rating, in-process.

Domain logic for turning a Telegram emoji reaction on a bot-sent image/video into
a 1..5 rating in the skchat ratings store. This is the port; host adapters (the
Hermes plugin, the skchat telegram_bridge) call it.

When the bot sends media, ``record_image_send`` maps ``(chat_id, message_id) ->
path`` in a JSONL log. When a user reacts, ``record_telegram_reaction`` resolves
the path, finds-or-creates the rating sidecar, and records the score — calling
``rating.record_render``/``record_score`` directly (same venv; no subprocess,
unlike the Hermes shim which couldn't import skchat).

Reaction map: 👎=1 🤷=2 👍=3 ❤️=4 🔥=5. Other emojis are ignored so casual
social reactions don't poison the rollup.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from . import rating

logger = logging.getLogger(__name__)

# Sent-map lives next to the ratings store so the whole rating dataset is one tree.
SENT_LOG = rating.RATINGS_DIR.parent / "reactions" / "sent.jsonl"

EMOJI_SCORE = {
    "👎": 1,
    "🤷": 2,
    "👍": 3,
    "❤": 4,
    "❤️": 4,
    "🔥": 5,
}

_lock = threading.Lock()


def score_from_emoji(emoji: str | None) -> Optional[int]:
    """Map a reaction emoji to a 1..5 score, or None if not a rating emoji."""
    if not emoji:
        return None
    return EMOJI_SCORE.get(emoji)


def record_image_send(chat_id, message_id, image_path: str) -> None:
    """Persist (chat_id, message_id) -> image_path so a later reaction resolves."""
    try:
        SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"chat_id": str(chat_id), "message_id": str(message_id),
               "image_path": str(image_path), "ts": time.time()}
        with _lock, SENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:
        logger.debug("record_image_send failed: %s", e)


def lookup_image_path(chat_id, message_id) -> Optional[str]:
    """Find the path the bot sent for this (chat_id, message_id), newest first."""
    if not SENT_LOG.exists():
        return None
    target = (str(chat_id), str(message_id))
    try:
        for line in reversed(SENT_LOG.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (row.get("chat_id"), row.get("message_id")) == target:
                return row.get("image_path")
    except Exception as e:
        logger.debug("lookup_image_path failed: %s", e)
    return None


def _image_id_for_path(image_path: str) -> Optional[str]:
    if not rating.RATINGS_DIR.exists():
        return None
    for sidecar in rating.RATINGS_DIR.glob("*.json"):
        try:
            data = json.loads(sidecar.read_text())
        except Exception:
            continue
        if data.get("image_path") == image_path:
            return data.get("image_id") or sidecar.stem
    return None


def record_telegram_reaction(chat_id, message_id, emoji: str,
                             note: str | None = None) -> Optional[int]:
    """Reaction → score. Returns the score on success, else None."""
    score = score_from_emoji(emoji)
    if score is None:
        return None
    image_path = lookup_image_path(chat_id, message_id)
    if not image_path:
        logger.debug("no image map for chat=%s msg=%s (emoji=%s)", chat_id, message_id, emoji)
        return None
    image_id = _image_id_for_path(image_path)
    if image_id is None:
        # Image was sent without a sidecar (e.g. an ad-hoc reply) — create one so
        # ANY posted image is ratable.
        rec = rating.record_render(image_path=image_path, prompt="", loras=[],
                                   extra={"source": "reaction-autocreate"})
        image_id = rec.image_id
    rec = rating.record_score(image_id, score, note=note, via="telegram")
    if rec is None:
        return None
    try:
        rating.write_rollup()
    except Exception as e:
        logger.debug("write_rollup failed: %s", e)
    logger.info("reaction rated path=%s score=%d emoji=%s", image_path, score, emoji)
    return score
