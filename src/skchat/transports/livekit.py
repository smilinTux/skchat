"""LiveKit transport — Lumina conversational agent over the VoiceEngine.

Phase-3 re-home of the lumina-call agent (previously the out-of-tree
``lumina-creative/scripts/lumina-call.py``) into skchat, sitting on top of the
unified ``skchat.voice_engine`` brain instead of inline STT/LLM/TTS/persona.

Design split (mirrors ``transports/websocket.py``):

* **VoiceEngine owns the brain** — persona + memory + forced-routing + LLM +
  tools. The transport calls ``engine.respond(...)`` for a turn and
  ``engine.stt`` / ``engine.tts`` for the audio legs.
* **This transport owns the room/turn loop** — per-participant energy VAD,
  barge-in, the addressing gate (who is Lumina actually being spoken to),
  the multi-agent roundtable turn-cap, and pushing PCM frames into a LiveKit
  ``LocalAudioTrack``.

The non-network decision logic (``VADSegmenter``, ``BargeInDetector``,
``AddressingGate``) is factored into pure, injectable-clock classes so it is
unit-testable without a live LiveKit room. See ``tests/test_transport_livekit.py``.

``livekit`` is a **soft dependency**: importing this module never requires the
``livekit`` SDK — only :func:`run_agent` / :func:`build_room_session` do. That
keeps the rest of skchat importable on hosts without the RTC stack (same policy
as ``livekit_routes.py``).

Environment (defaults match the live tailnet stack; ``SKVOICE_*`` feed the
engine via :class:`skchat.voice_engine.config.VoiceConfig`):

    SKCHAT_LIVEKIT_DEFAULT_ROOM   lumina-and-chef
    LUMINA_IDENTITY               lumina
    LUMINA_NAME                   Lumina
    LUMINA_VAD_RMS                1200   (int16 RMS speech gate)
    LUMINA_BARGE_IN               1      (0/false disables barge-in)
    LUMINA_BARGE_IN_DWELL_MS      300
    LUMINA_BARGE_IN_RMS           2000
    LUMINA_FOLLOW_UP_S            60     (roundtable follow-up window)
    LUMINA_AGENT_TURN_CAP         6      (consecutive peer-agent replies)
    LUMINA_OPERATOR_PREFIXES      chef   (comma list of Chef identity prefixes)
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from typing import Callable, Iterable, Optional

log = logging.getLogger("skchat.transports.livekit")

# ─── Audio / VAD tuning (ported verbatim from lumina-call.py) ───────────────
STT_SAMPLE_RATE = 16000          # whisper-friendly, 16 kHz mono int16
VAD_FRAME_MS = 20
RMS_VOICE_THRESHOLD = int(os.getenv("LUMINA_VAD_RMS", "1200"))
SILENCE_HANGOVER_MS = 800        # trailing silence that ends an utterance
MIN_UTTERANCE_MS = 600           # ignore short blips / "uh"s
MAX_UTTERANCE_MS = 12000         # force-flush so a monologue doesn't starve
ECHO_TAIL_S = float(os.getenv("LUMINA_ECHO_TAIL_S", "2.5"))

# Barge-in — cut Lumina off when the user starts talking during her reply.
BARGE_IN_ENABLED = os.getenv("LUMINA_BARGE_IN", "1") not in ("0", "false", "no", "")
BARGE_IN_DWELL_MS = int(os.getenv("LUMINA_BARGE_IN_DWELL_MS", "300"))
BARGE_IN_RMS = int(os.getenv("LUMINA_BARGE_IN_RMS", "2000"))

# Roundtable / addressing tuning.
FOLLOW_UP_WINDOW_S = float(os.getenv("LUMINA_FOLLOW_UP_S", "60"))
AGENT_TURN_CAP = int(os.getenv("LUMINA_AGENT_TURN_CAP", "6"))
DEDUP_WINDOW_S = 3.0

IDENTITY = os.getenv("LUMINA_IDENTITY", "lumina")
DISPLAY_NAME = os.getenv("LUMINA_NAME", "Lumina")
DEFAULT_ROOM = os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")

_CHEF_IDENTITY_PREFIXES = tuple(
    p.strip().lower()
    for p in os.getenv("LUMINA_OPERATOR_PREFIXES", "chef").split(",")
    if p.strip()
)

# Wake words — Lumina's name + common whisper mis-transcriptions + generic
# direct-address phrases. Ported from lumina-call.py so behaviour matches.
ADDRESS_TRIGGERS = (
    DISPLAY_NAME.lower(), IDENTITY.lower(),
    f"hey {DISPLAY_NAME.lower()}", f"okay {DISPLAY_NAME.lower()}", f"ok {DISPLAY_NAME.lower()}",
    "lumina", "luminess", "luminous", "lumi", "loomina", "lumino", "luna",
    "loma", "luma", "lamina", "ramona", "ramina", "lemina", "lumena",
    "lumeena", "lumenia", "lemonade", "lou mina", "lou meena",
    "limit of", "live mina", "live meena", "loomi", "loo mina",
    "hey lumina", "okay lumina", "ok lumina",
    "are you there", "you there",
    "you listening", "are you listening",
    "you hear me", "do you hear",
    "can you hear",
    "hey there", "hello there",
    "what about you", "what do you think",
    "tell me",
)
_ADDRESS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in ADDRESS_TRIGGERS) + r")\b", re.I
)


# ─── Helpers ────────────────────────────────────────────────────────────────
def rms16(pcm: bytes) -> float:
    """Root-mean-square amplitude of signed 16-bit little-endian mono PCM.

    Prefers the stdlib ``audioop`` (fast C); falls back to a pure-python
    computation so the module works on Python builds where ``audioop`` was
    removed (3.13+ without the shim).
    """
    if not pcm:
        return 0.0
    try:  # pragma: no cover - environment dependent
        import audioop

        return float(audioop.rms(pcm, 2))
    except Exception:
        pass
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0
    for i in range(0, n * 2, 2):
        s = pcm[i] | (pcm[i + 1] << 8)
        if s >= 0x8000:
            s -= 0x10000
        total += s * s
    return math.sqrt(total / n)


def is_chef_identity(identity: str, prefixes: Iterable[str] = _CHEF_IDENTITY_PREFIXES) -> bool:
    """True when ``identity`` is one of Chef's devices (chef-laptop, chef-phone…)."""
    ident_low = (identity or "").lower()
    return any(ident_low.startswith(p) for p in prefixes)


# ─── VAD segmenter (energy gate; no torch, no network) ──────────────────────
class VADSegmenter:
    """Per-participant energy VAD → utterance segmentation.

    Feed 16 kHz mono int16 PCM frames via :meth:`push`; it returns the joined
    utterance PCM when a speech segment completes (trailing-silence hangover or
    max-length force-flush), else ``None``. Short blips below
    ``min_utterance_ms`` are dropped (return ``None``).

    Pure logic ported from ``lumina-call.py:listen_to_participant`` — same
    thresholds, same state machine — with an injectable ``clock`` for tests.
    """

    def __init__(
        self,
        *,
        rms_threshold: int = RMS_VOICE_THRESHOLD,
        silence_hangover_ms: int = SILENCE_HANGOVER_MS,
        min_utterance_ms: int = MIN_UTTERANCE_MS,
        max_utterance_ms: int = MAX_UTTERANCE_MS,
        frame_ms: int = VAD_FRAME_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rms_threshold = rms_threshold
        self.silence_hangover_ms = silence_hangover_ms
        self.min_utterance_ms = min_utterance_ms
        self.max_utterance_ms = max_utterance_ms
        self.frame_ms = frame_ms
        self._clock = clock
        self._in_utterance = False
        self._voiced: list[bytes] = []
        self._last_voice_t = 0.0
        self._utterance_start_t = 0.0

    def reset(self) -> None:
        self._in_utterance = False
        self._voiced.clear()

    def push(self, frame: bytes, *, gated: bool = False) -> Optional[bytes]:
        """Consume one audio frame.

        Args:
            frame: raw int16-LE mono PCM for one ``frame_ms`` window.
            gated: True while Lumina is speaking / in her echo tail — the mic
                is ignored (mirrors ``speaker.is_speaking or in_echo_tail``).

        Returns:
            Joined utterance PCM bytes when a segment completes and is long
            enough, else ``None``.
        """
        if gated:
            if self._in_utterance:
                self.reset()
            return None

        now = self._clock()
        level = rms16(frame)

        if level >= self.rms_threshold:
            if not self._in_utterance:
                self._in_utterance = True
                self._utterance_start_t = now
                self._voiced = []
            self._voiced.append(frame)
            self._last_voice_t = now
        elif self._in_utterance:
            self._voiced.append(frame)  # keep trailing silence in the clip

        if not self._in_utterance:
            return None

        silent_ms = (now - self._last_voice_t) * 1000.0
        duration_ms = (now - self._utterance_start_t) * 1000.0
        if silent_ms >= self.silence_hangover_ms or duration_ms >= self.max_utterance_ms:
            self._in_utterance = False
            pcm = b"".join(self._voiced)
            self._voiced = []
            if duration_ms >= self.min_utterance_ms:
                return pcm
        return None


# ─── Barge-in detector ──────────────────────────────────────────────────────
class BargeInDetector:
    """Sustained-voice detector used only while Lumina is speaking.

    Accumulates ``frame_ms`` of voiced time each frame whose RMS clears the
    elevated ``rms_threshold`` (and decays it otherwise). :meth:`push` returns
    True once accumulated voiced time reaches ``dwell_ms`` — the caller then
    cancels Lumina's current speak task. Ported from the barge-in block of
    ``lumina-call.py:listen_to_participant``.
    """

    def __init__(
        self,
        *,
        rms_threshold: int = BARGE_IN_RMS,
        dwell_ms: int = BARGE_IN_DWELL_MS,
        frame_ms: int = VAD_FRAME_MS,
        enabled: bool = BARGE_IN_ENABLED,
    ) -> None:
        self.rms_threshold = rms_threshold
        self.dwell_ms = dwell_ms
        self.frame_ms = frame_ms
        self.enabled = enabled
        self._voiced_ms = 0.0

    def reset(self) -> None:
        self._voiced_ms = 0.0

    def push(self, frame: bytes) -> bool:
        if not self.enabled:
            return False
        if rms16(frame) >= self.rms_threshold:
            self._voiced_ms += self.frame_ms
            if self._voiced_ms >= self.dwell_ms:
                self._voiced_ms = 0.0
                return True
        else:
            self._voiced_ms = max(0.0, self._voiced_ms - self.frame_ms)
        return False


# ─── Addressing gate + roundtable turn-cap ──────────────────────────────────
class AddressingGate:
    """Decides whether an utterance is actually directed at this agent, and
    damps multi-agent ping-pong (the "roundtable").

    Ported from ``lumina-call.py`` (`_is_addressed` + the agent-turn-cap block
    of `handle_utterance`). ``clock`` is injectable for deterministic tests.

    Rules (in order), from :meth:`is_addressed`:
      1. Named another agent (and not me) → not for me.
      2. Named me → engage (open my follow-up window with this speaker).
      3. Sacred mode + speaker is Chef → everything Chef says is to me.
      4. I recently engaged THIS speaker (< follow_up_window_s) → keep rolling.
      5. Generic wake-word AND no other agent present → engage.
      6. A peer agent spoke within my broadcast window → engage (roundtable).

    :meth:`should_reply` layers the loop-damping cap on top: a human turn
    resets the streak; each consecutive peer-agent reply increments it; past
    ``agent_turn_cap`` the agents go quiet until a human speaks again.
    """

    def __init__(
        self,
        *,
        identity: str = IDENTITY,
        display_name: str = DISPLAY_NAME,
        chef_prefixes: Iterable[str] = _CHEF_IDENTITY_PREFIXES,
        follow_up_window_s: float = FOLLOW_UP_WINDOW_S,
        agent_turn_cap: int = AGENT_TURN_CAP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._my_names = [n.lower() for n in (identity, display_name) if n]
        self._chef_prefixes = tuple(p.lower() for p in chef_prefixes)
        self.follow_up_window_s = follow_up_window_s
        self.agent_turn_cap = agent_turn_cap
        self._clock = clock
        self._engaged_with: dict[str, float] = {}
        self._broadcast_speak_t = 0.0
        self._agent_turn_streak = 0

    # -- helpers -------------------------------------------------------------
    def _is_chef(self, speaker_id: str) -> bool:
        return is_chef_identity(speaker_id, self._chef_prefixes)

    def note_own_speech(self) -> None:
        """Call whenever this agent speaks — opens the broadcast follow-up
        window so a peer agent's next turn drives the roundtable."""
        self._broadcast_speak_t = self._clock()

    def note_reply_to(self, speaker_id: str) -> None:
        """Record that we just replied to ``speaker_id`` (keeps their
        un-named follow-ups rolling to us)."""
        self._engaged_with[speaker_id] = self._clock()

    # -- decisions -----------------------------------------------------------
    def is_addressed(
        self, speaker_id: str, text: str, *, mode: str = "sacred", other_agents: Iterable[str] = ()
    ) -> bool:
        t = (text or "").lower()
        others = [o.lower() for o in other_agents if o]
        named_me = any(re.search(rf"\b{re.escape(n)}\b", t) for n in self._my_names)
        named_other = any(re.search(rf"\b{re.escape(n)}\b", t) for n in others)

        if named_other and not named_me:
            return False
        if named_me:
            self._engaged_with[speaker_id] = self._clock()
            return True
        if mode == "sacred" and self._is_chef(speaker_id):
            self._engaged_with[speaker_id] = self._clock()
            return True
        last = self._engaged_with.get(speaker_id)
        if last is not None and self._clock() - last < self.follow_up_window_s:
            return True
        if not others and _ADDRESS_RE.search(text or ""):
            self._engaged_with[speaker_id] = self._clock()
            return True
        if (
            not self._is_chef(speaker_id)
            and self._clock() - self._broadcast_speak_t < self.follow_up_window_s
        ):
            self._engaged_with[speaker_id] = self._clock()
            return True
        return False

    def should_reply(
        self, speaker_id: str, text: str, *, mode: str = "sacred", other_agents: Iterable[str] = ()
    ) -> bool:
        """Full gate: addressing + roundtable loop-damping.

        Returns True only when the agent should actually take this turn.
        Side effects: mutates the engaged/streak state exactly as the live
        agent does, so repeated calls model a real conversation.
        """
        addressed = self.is_addressed(speaker_id, text, mode=mode, other_agents=other_agents)
        if self._is_chef(speaker_id):
            self._agent_turn_streak = 0
        if not addressed:
            return False
        if not self._is_chef(speaker_id):
            self._agent_turn_streak += 1
            if self._agent_turn_streak > self.agent_turn_cap:
                log.info("agent-turn cap (%d) hit — quiet until a human speaks", self.agent_turn_cap)
                return False
        return True


# ─── Transcript de-dup (multi-tab / whisper-repetition guards) ──────────────
class TranscriptDedup:
    """Drops multi-tab duplicate transcripts and whisper repetition spam.

    Ported from the dedup guards in ``handle_utterance``. Pure + clock-injectable.
    """

    def __init__(self, *, window_s: float = DEDUP_WINDOW_S, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._recent: list[tuple[str, float]] = []

    def is_duplicate(self, text: str) -> bool:
        now = self._clock()
        normalized = (text or "").lower().strip().rstrip(".,!?")
        self._recent = [(t, ts) for (t, ts) in self._recent if now - ts < self.window_s]
        if any(t == normalized for (t, _ts) in self._recent):
            return True
        self._recent.append((normalized, now))
        return False

    @staticmethod
    def is_whisper_repetition(text: str) -> bool:
        """True for the 'If If If If…' / 'Bye. Bye. Bye.' repetition pattern."""
        words = (text or "").split()
        if len(words) < 6:
            return False
        lowers = [w.lower().strip(".,!?\"'") for w in words]
        top = max(set(lowers), key=lowers.count)
        return lowers.count(top) >= len(words) * 0.6 and len(top) <= 4


# ─── Engine factory (mirrors websocket transport) ───────────────────────────
def default_engine_factory() -> Callable[[str], object]:
    """Build a real :class:`~skchat.voice_engine.engine.VoiceEngine` per agent.

    Same construction the WebSocket transport uses, so both transports share
    one brain/config/tool-registry. Imported lazily so this module stays cheap.
    """

    def factory(agent_name: str):
        from skchat.voice_engine.builtin_tools import build_default_registry  # noqa: PLC0415
        from skchat.voice_engine.config import VoiceConfig  # noqa: PLC0415
        from skchat.voice_engine.engine import VoiceEngine  # noqa: PLC0415
        from skchat.voice_engine.stt import STTClient  # noqa: PLC0415
        from skchat.voice_engine.tts import TTSClient  # noqa: PLC0415

        cfg = VoiceConfig.from_env()
        registry = build_default_registry(cfg, agent_name)
        # LiveKit needs the audio legs on the engine (unlike the pure-brain
        # default) so the transport can call engine.stt / engine.tts.
        return VoiceEngine(
            cfg,
            agent_name,
            stt=STTClient(cfg),
            tts=TTSClient(cfg),
            registry=registry,
        )

    return factory


# ─── Turn orchestration over the engine ─────────────────────────────────────
async def run_turn(
    engine,
    history: list[dict],
    transcript: str,
    *,
    mode: str,
    speaker_id: str,
    is_operator: bool,
) -> str:
    """Route one utterance through the VoiceEngine brain and append to history.

    STT is done by the caller (needs the raw PCM + LiveKit audio buffer); this
    is the LLM/persona/tool leg + history maintenance, identical in shape to
    the WebSocket transport's ``_process_speech`` tail.
    """
    reply = await engine.respond(
        transcript, history, mode=mode, speaker_id=speaker_id, is_operator=is_operator
    )
    history.append({"role": "user", "content": transcript})
    history.append({"role": "assistant", "content": reply})
    if len(history) > 40:
        history[:] = history[-30:]
    return reply


# ─── LiveKit wiring (soft dep) ──────────────────────────────────────────────
def _require_livekit():
    try:
        from livekit import rtc  # noqa: PLC0415

        return rtc
    except Exception as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "livekit SDK not installed — `pip install livekit livekit-api` to run "
            "the LiveKit transport (the rest of skchat works without it)."
        ) from exc


def mode_ceiling(room_name: str) -> str:
    """Room name sets the *maximum* mode; a stranger joining still forces group.

    Unknown rooms default to 'group' for safety (ported from
    ``lumina-call.py:_room_mode_ceiling``).
    """
    ceilings = {"lumina-and-chef": "sacred"}
    return ceilings.get((room_name or "").strip(), "group")


__all__ = [
    "VADSegmenter",
    "BargeInDetector",
    "AddressingGate",
    "TranscriptDedup",
    "rms16",
    "is_chef_identity",
    "mode_ceiling",
    "default_engine_factory",
    "run_turn",
    "ADDRESS_TRIGGERS",
]
