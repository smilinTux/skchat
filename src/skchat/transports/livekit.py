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

import asyncio
import json
import logging
import math
import os
import re
import time
from typing import Callable, Iterable, Optional

from skchat.voice_engine.tools import pick_filler, pick_waiting_filler

log = logging.getLogger("skchat.transports.livekit")

# ─── Audio / VAD tuning (ported verbatim from lumina-call.py) ───────────────
STT_SAMPLE_RATE = 16000  # whisper-friendly, 16 kHz mono int16
#: Sample rate we PUBLISH at. Piper renders 22.05 kHz but the engine's TTS
#: client resamples to this, so the capture frames stay a single rate.
TTS_SAMPLE_RATE = int(os.getenv("LUMINA_TTS_SAMPLE_RATE", "16000"))
VAD_FRAME_MS = 20
RMS_VOICE_THRESHOLD = int(os.getenv("LUMINA_VAD_RMS", "1200"))
SILENCE_HANGOVER_MS = 800  # trailing silence that ends an utterance
MIN_UTTERANCE_MS = 600  # ignore short blips / "uh"s
MAX_UTTERANCE_MS = 12000  # force-flush so a monologue doesn't starve
ECHO_TAIL_S = float(os.getenv("LUMINA_ECHO_TAIL_S", "2.5"))

# Barge-in — cut Lumina off when the user starts talking during her reply.
#
# Thresholds are deliberately well above the speech gate. On a phone with the
# speaker on there is no echo cancellation between her output and its mic, so
# HER OWN VOICE comes back as "the user talking" and she interrupts herself.
# Observed live 2026-08-13: barge-in firing 1-6s into every single reply, which
# also chopped the front off Chef's next sentence ("to you available", "tools to
# get available") and left her answering fragments.
BARGE_IN_ENABLED = os.getenv("LUMINA_BARGE_IN", "1") not in ("0", "false", "no", "")
BARGE_IN_DWELL_MS = int(os.getenv("LUMINA_BARGE_IN_DWELL_MS", "600"))
BARGE_IN_RMS = int(os.getenv("LUMINA_BARGE_IN_RMS", "3500"))
#: Ignore barge-in for this long after she starts a reply. Her own onset is the
#: loudest thing the far-end mic hears, and it arrives immediately.
BARGE_IN_GRACE_MS = int(os.getenv("LUMINA_BARGE_IN_GRACE_MS", "1200"))

# Reply loudness. Different TTS engines hand back wildly different levels for
# the same words: Piper peak-normalizes (peak 32767, RMS ~5800) while F5-TTS,
# which renders Lumina's actual cloned voice, comes back at peak 19226 / RMS
# ~2021 — nearly 3x quieter. Publishing that raw is what "her voice is way too
# low" sounds like, and it is a property of the engine, not of the call. So
# normalize here, once, where every reply passes regardless of backend.
TTS_TARGET_PEAK = float(os.getenv("LUMINA_TTS_TARGET_PEAK", "0.97"))
TTS_EXTRA_GAIN = float(os.getenv("LUMINA_TTS_GAIN", "1.0"))

# "I heard you, working on it" filler.
#
# Fires on measured WAIT, not on guessed intent. The first cut gated it behind
# wants_narrate/wants_action keywords, so ordinary conversation never triggered
# one and Chef got the silence back ("im chatting, no filler"). A keyword is a
# guess about whether a turn will be slow; the clock is the actual answer, and
# it also stays quiet when she happens to reply instantly.
FILLER_ENABLED = os.getenv("LUMINA_FILLER", "1") not in ("0", "false", "no", "")
#: Speak the acknowledgement once a reply has taken longer than this.
FILLER_DELAY_S = float(os.getenv("LUMINA_FILLER_DELAY_S", "0.8"))
#: Speak it immediately on every turn, no matter how fast the reply is.
FILLER_ALWAYS = os.getenv("LUMINA_FILLER_ALWAYS", "0") not in ("0", "false", "no", "")
#: Repeat a short "still working on it" every this many seconds while a turn is
#: still running. A narration can take 30s to generate and minutes to render,
#: and silence that long is indistinguishable from a dropped call.
FILLER_REPEAT_S = float(os.getenv("LUMINA_FILLER_REPEAT_S", "12"))

#: Longest text handed to TTS in one request. Beyond this a reply is spoken in
#: sentence-aligned chunks so audio starts within seconds instead of after the
#: whole narration renders, and so no single request can hit the TTS timeout.
LONGFORM_CHUNK_CHARS = int(os.getenv("LUMINA_LONGFORM_CHUNK_CHARS", "400"))

# Avatar placeholder video. A voice call with no video track shows the operator
# a blank tile, which reads as "not connected" even while she is talking. This
# publishes her portrait as a still video track so there is a face on the call.
# Explicitly a PLACEHOLDER, the same call skvoice's facetime.py made: a real
# talking head (MuseTalk or similar) is separate work with its own GPU budget,
# and swapping the frame source for a renderer changes nothing else here.
AVATAR_ENABLED = os.getenv("LUMINA_AVATAR", "1") not in ("0", "false", "no", "")
AVATAR_IMAGE = os.getenv(
    "LUMINA_AVATAR_IMAGE",
    os.path.expanduser(
        "~/.skcapstone/agents/%s/avatar/portrait.png" % os.getenv("SKAGENT", "lumina")
    ),
)
#: Frame rate for the still. Low on purpose: the picture never changes, and the
#: only reason to resend at all is that WebRTC treats a stalled track as frozen.
AVATAR_FPS = float(os.getenv("LUMINA_AVATAR_FPS", "2"))
#: Longest edge, downscaled from the source portrait. 720 stays recognisable on
#: a phone without spending call bandwidth on a still image.
AVATAR_MAX_EDGE = int(os.getenv("LUMINA_AVATAR_MAX_EDGE", "720"))

#: End a session once the room has been empty this long. Rooms are derived
#: per-pair and therefore reused by every future call, so a session that
#: outlives its call does not merely leak: it answers the NEXT call with pumps
#: bound to participants who left.
SESSION_EMPTY_TIMEOUT_S = float(os.getenv("LUMINA_SESSION_EMPTY_TIMEOUT_S", "60"))

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
    DISPLAY_NAME.lower(),
    IDENTITY.lower(),
    f"hey {DISPLAY_NAME.lower()}",
    f"okay {DISPLAY_NAME.lower()}",
    f"ok {DISPLAY_NAME.lower()}",
    "lumina",
    "luminess",
    "luminous",
    "lumi",
    "loomina",
    "lumino",
    "luna",
    "loma",
    "luma",
    "lamina",
    "ramona",
    "ramina",
    "lemina",
    "lumena",
    "lumeena",
    "lumenia",
    "lemonade",
    "lou mina",
    "lou meena",
    "limit of",
    "live mina",
    "live meena",
    "loomi",
    "loo mina",
    "hey lumina",
    "okay lumina",
    "ok lumina",
    "are you there",
    "you there",
    "you listening",
    "are you listening",
    "you hear me",
    "do you hear",
    "can you hear",
    "hey there",
    "hello there",
    "what about you",
    "what do you think",
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
                log.info(
                    "agent-turn cap (%d) hit — quiet until a human speaks", self.agent_turn_cap
                )
                return False
        return True


# ─── Transcript de-dup (multi-tab / whisper-repetition guards) ──────────────
class TranscriptDedup:
    """Drops multi-tab duplicate transcripts and whisper repetition spam.

    Ported from the dedup guards in ``handle_utterance``. Pure + clock-injectable.
    """

    def __init__(
        self, *, window_s: float = DEDUP_WINDOW_S, clock: Callable[[], float] = time.monotonic
    ):
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
        # Built-ins only here. The MCP surface is attached later, inside the
        # live session, because connect_all() is async and its servers must be
        # torn down when the call ends: this factory is sync and per-process.
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


def mode_ceiling(room_name: str, peer_fqid: str | None = None) -> str:
    """The *maximum* mode for a call; a stranger joining still forces group.

    Prefer the verified peer identity, fall back to the room name.

    The room-name form is a trap inherited from ``lumina-call.py`` and kept only
    for the legacy fixed room. skchat summons calls into rooms named by
    :func:`skchat.call_session.derive_room`, which are opaque hashes like
    ``call-e4qj4kxvef2dxmxq``. Keying "sacred" off a literal name therefore
    downgrades EVERY real 1:1 with Chef to the group register, and any
    Chef-only tool authorisation keyed on mode misfires with it.

    So when the caller knows who it is actually talking to (the answerer does:
    the invite is signature-verified and carries ``from_fqid``), pass it and the
    identity decides. ``peer_fqid`` accepts a bare short name or a full FQID.

    Unknown peers and unknown rooms both default to 'group', which is the safe
    direction: a wrong 'group' loses warmth, a wrong 'sacred' leaks it.
    """
    if peer_fqid:
        if is_chef_identity(str(peer_fqid).split("@")[0]):
            return "sacred"
        return "group"
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
    "build_room_session",
    "run_agent",
    "wav_to_pcm",
    "engine_mode",
    "default_engine_factory",
    "run_turn",
    "ADDRESS_TRIGGERS",
]


# ─── Room session (the loop the module docstring promised) ──────────────────
#: The transport's ceiling vocabulary is sacred/group (inherited from
#: lumina-call.py). The VoiceEngine persona speaks private/group. They are the
#: same idea under different names, and "sacred" is NOT a value the persona
#: understands: it fell through to the group branch, which injects "This is a
#: group call ... no private topics." That is why Chef, alone with her on a 1:1,
#: was met by an agent behaving as if she were on a public conference call and
#: refusing to discuss anything private.
_ENGINE_MODE = {"sacred": "private", "private": "private", "group": "group"}


def engine_mode(ceiling: str) -> str:
    """Translate a transport mode ceiling into the persona's vocabulary."""
    return _ENGINE_MODE.get((ceiling or "").strip().lower(), "group")


def wav_to_pcm(data: bytes, target_rate: int) -> bytes:
    """Decode a WAV payload to raw mono int16 PCM at *target_rate*.

    The TTS client returns a complete WAV (Piper renders 22.05 kHz), and it does
    NOT resample. Publishing those bytes straight into an AudioFrame declared at
    16 kHz played the 44-byte RIFF header as a click and ran the speech at
    0.73x, pitched down: the "really slow and creepy" voice Chef heard. An
    earlier comment in this file claimed the client resampled; it does not, and
    I should have checked rather than asserted it.

    Falls back to returning the input unchanged if it is not WAV (already raw
    PCM), so a different TTS backend still works.
    """
    if not data[:4] == b"RIFF":
        return data
    import io
    import wave

    with wave.open(io.BytesIO(data)) as w:
        rate, chans, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())

    if width != 2:  # pragma: no cover - Piper is 16-bit
        log.warning("unexpected TTS sample width %d; passing through", width)
        return pcm
    if chans > 1:  # pragma: no cover - Piper is mono
        try:
            import audioop

            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        except Exception:
            pass
    if rate == target_rate:
        return pcm
    try:
        import audioop

        converted, _ = audioop.ratecv(pcm, 2, 1, rate, target_rate, None)
        return converted
    except Exception:
        # No audioop (3.13+): publish at the source rate rather than at the
        # wrong one. Slightly off is recoverable; 0.73x is not.
        log.warning("cannot resample %d->%d; audio may play at the wrong speed", rate, target_rate)
        return pcm


def normalize_pcm(
    pcm: bytes,
    *,
    target_peak: float = TTS_TARGET_PEAK,
    extra_gain: float = TTS_EXTRA_GAIN,
) -> bytes:
    """Bring one reply up to a consistent loudness, whatever rendered it.

    Peak-normalizes to ``target_peak`` of full scale, then applies
    ``extra_gain``. Peak rather than RMS because peak cannot clip on its own:
    an RMS target on speech with a couple of loud plosives would drive most of
    the waveform into the limiter. ``extra_gain`` above 1.0 deliberately can
    clip, so it stays opt-in and defaults off.

    Returns the input unchanged on digital silence (nothing to scale to) or if
    audioop is unavailable (Python 3.13 dropped it), because a quiet reply
    beats no reply.
    """
    if not pcm:
        return pcm
    try:
        import audioop
    except Exception:  # pragma: no cover - 3.13+ without audioop
        return pcm
    peak = audioop.max(pcm, 2)
    if peak <= 0:
        return pcm
    factor = (target_peak * 32767.0) / peak * extra_gain
    if abs(factor - 1.0) < 0.01:
        return pcm
    # audioop.mul saturates rather than wrapping, so a hot factor limits
    # instead of turning into noise.
    return audioop.mul(pcm, 2, factor)


def _engine_voice(engine) -> str:
    """The TTS voice this engine is configured for.

    Read off the engine rather than hardcoded, so transport and brain cannot
    disagree. Falls back to the Piper default if the engine exposes no config.
    """
    cfg = getattr(engine, "config", None) or getattr(engine, "cfg", None)
    voice = getattr(cfg, "tts_voice", None) if cfg else None
    return voice or os.getenv("SKCHAT_TTS_VOICE", "af_heart")


# Whisper's labels for "there was no speech here", plus the single tokens it
# emits on room tone. Bracketed/parenthesised forms are matched structurally so
# new variants ("[ Silence ]", "(coughs)") do not need enumerating.
_NON_SPEECH_EXACT = frozenset(
    {
        "you",
        "thank you",
        "thanks for watching",
        "thanks for watching!",
        "bye",
        ".",
        "...",
    }
)


def is_non_speech(transcript: str) -> bool:
    """True when a transcript is whisper describing silence, not words spoken.

    Whisper does not return an empty string for a silent segment; it returns a
    label such as ``[BLANK_AUDIO]`` or ``(silence)``, or a stock filler like
    "you" / "Thanks for watching" learned from captioned video. Treating those
    as user speech makes the agent answer nobody.
    """
    t = (transcript or "").strip()
    if not t:
        return True
    if (t.startswith("[") and t.endswith("]")) or (t.startswith("(") and t.endswith(")")):
        return True
    bare = t.lower().strip(" .!,?-")
    # Punctuation-only ("...", "?!") strips to nothing: no words were said.
    return not bare or bare in _NON_SPEECH_EXACT


def load_avatar_rgba(path: str = AVATAR_IMAGE, max_edge: int = AVATAR_MAX_EDGE):
    """Load the portrait as ``(width, height, rgba_bytes)``, or ``None``.

    Returns None rather than raising for every reason it could fail (no Pillow,
    no file, corrupt image): a missing face must degrade to a voice-only call,
    never to a failed one.
    """
    try:
        from PIL import Image  # noqa: PLC0415 - optional dependency
    except Exception as exc:  # pragma: no cover - Pillow not installed
        log.info("no Pillow; skipping avatar video (%s)", exc)
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGBA")
            w, h = im.size
            scale = min(1.0, max_edge / float(max(w, h)))
            if scale < 1.0:
                # Even dimensions: odd sizes break chroma subsampling on the
                # encoder's I420 conversion.
                w, h = (int(w * scale) // 2) * 2, (int(h * scale) // 2) * 2
                im = im.resize((w, h), Image.LANCZOS)
            return w, h, im.tobytes()
    except Exception as exc:  # noqa: BLE001 - a bad image must not fail the call
        log.warning("avatar image unusable at %s (%r); voice only", path, exc)
        return None


def split_for_speech(text: str, max_chars: int = LONGFORM_CHUNK_CHARS) -> list[str]:
    """Split a long reply into speakable chunks on sentence boundaries.

    Synthesis is proportional to length, so rendering a whole narration before
    saying a word means minutes of silence, and past the TTS timeout it means
    NO word at all: a 3400-character worship story rendered fine and then died
    on the wire, logging "reply ready in 60.02s (0.0s of audio)".

    Chunking turns that into speech starting a few seconds in, and it removes
    the timeout cliff entirely because no single request is ever large.

    Splits only between sentences, never mid-sentence, so each chunk is a
    natural place for a breath. Short replies come back as a single chunk and
    are unaffected.
    """
    t = (text or "").strip()
    if len(t) <= max_chars:
        return [t] if t else []
    # Keep the terminator with its sentence.
    parts = re.split(r"(?<=[.!?…])\s+", t)
    chunks: list[str] = []
    cur = ""
    for p in parts:
        if cur and len(cur) + 1 + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _drain(source) -> None:
    """Throw away audio already queued for playout. Best effort.

    Needed on barge-in: the publish buffer holds up to a second of the sentence
    being cancelled, and without dropping it that second still reaches the
    listener, ahead of whatever she says next.
    """
    try:
        source.clear_queue()
    except Exception as exc:  # noqa: BLE001 - older SDKs may not expose it
        log.debug("clear_queue unavailable (%r)", exc)


def _preview(text: str, n: int = 60) -> str:
    """Short, log-safe echo of a reply."""
    return repr((text or "").strip()[:n])


async def _set_state(room, state: str, detail: str = "") -> None:
    """Publish idle/listening/thinking/speaking as participant metadata.

    The webui surfaces this as a status pill, which is how the operator can tell
    "she is working on it" apart from "she is broken". Ported from
    ``lumina-call.py:set_state``, including the part that matters: this is
    FIRE-AND-FORGET. ``set_metadata`` can take seconds when the signal channel is
    busy publishing audio, and awaiting it before kicking off the LLM is real,
    measured latency on every single turn. Log intent now, push in background.
    """
    log.info("state -> %s%s", state, f" ({detail[:60]})" if detail else "")
    if room is None:
        return

    async def _push() -> None:
        try:
            await room.local_participant.set_metadata(
                json.dumps({"state": state, "detail": detail[:120]})
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a call
            log.debug("set_metadata failed (%r)", exc)

    try:
        asyncio.create_task(_push())
    except RuntimeError:  # pragma: no cover - no running loop (tests)
        pass


async def _emit_level(room, speaker_id: str, peak: float) -> None:
    """Publish a mic-level datagram so a client can show "I can hear you".

    Chef asked for exactly this: with no feedback, a call where the gate is set
    too high is indistinguishable from a broken pipeline. Best effort; telemetry
    must never disturb the call.
    """
    try:
        payload = json.dumps(
            {
                "type": "level",
                "speaker": speaker_id,
                "rms": round(peak),
                "gate": RMS_VOICE_THRESHOLD,
                "hearing": peak >= RMS_VOICE_THRESHOLD,
            }
        ).encode()
        await room.local_participant.publish_data(payload, reliable=False)
    except Exception:  # pragma: no cover - telemetry only
        log.debug("level telemetry publish failed", exc_info=True)


async def build_room_session(
    room_name: str,
    *,
    url: str,
    token: str,
    agent_name: str = "lumina",
    peer_fqid: str | None = None,
    engine_factory: Callable[[str], object] | None = None,
):
    """Join *room_name* and converse until the room closes.

    This is the piece that was missing. Everything above it (VAD, barge-in,
    addressing, dedup, ``run_turn``) was already here and unit-tested, and
    ``skchat`` already knew which room to be in via
    :func:`skchat.call_session.derive_room`. What did not exist was anything
    that put the brain *in* that room, so the process that joined
    (``call_answerer``) published silence while the process that could talk
    (``lumina-call.py``) sat in a different, fixed room.

    The mode is decided by *peer_fqid* when the caller knows it. The answerer
    does: the invite is signature-verified. See :func:`mode_ceiling` for why the
    room name alone is the wrong key.

    Returns when the room disconnects. Raises RuntimeError if the livekit SDK is
    absent, so a host without the RTC stack fails loudly here rather than
    silently importing.
    """
    rtc = _require_livekit()
    engine = (engine_factory or default_engine_factory())(agent_name)
    mode = mode_ceiling(room_name, peer_fqid)

    room = rtc.Room()
    history: list[dict] = []
    gate = AddressingGate()
    dedup = TranscriptDedup()
    segmenters: dict[str, VADSegmenter] = {}
    speaking = asyncio.Event()  # set while we are playing TTS
    # Sustained-voice detectors, one per speaker, consulted only while she is
    # speaking. The class existed and was unit-tested but was never instantiated,
    # so she could not be interrupted at all.
    bargers: dict[str, BargeInDetector] = {}
    # The in-flight turn, so barge-in has something to cancel. handle_utterance
    # used to be awaited inline in the pump, which meant the pump stopped reading
    # frames for the whole turn: even a wired-up detector would have been fed
    # nothing while she talked.
    turn: dict[str, asyncio.Task | None] = {"task": None}
    #: When the current reply started playing, for the barge-in grace window.
    speak_started: dict[str, float] = {"t": 0.0}

    source = rtc.AudioSource(TTS_SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track(f"{agent_name}-voice", source)

    async def synth(text: str) -> bytes:
        """Render one reply to publishable PCM. No side effects on call state.

        Split out of ``say`` so a reply can be SYNTHESIZED while the filler is
        still being spoken. Synthesis used to start only after the filler
        finished playing, which meant the "working on it" acknowledgement made
        the real answer ~2.4s later than it needed to be: it bought perceived
        responsiveness and paid for it in actual answer time. Measured on a live
        turn 2026-08-13 -- LLM done at t+1.3s, reply audio not ready until
        t+6.3s, with 2.5s of that spent waiting on the filler and 2.4s on a TTS
        call that could have run during it.
        """
        if not text.strip():
            return b""
        # `voice` is keyword-only and required. Take it from the engine's own
        # config so the transport never picks a voice the engine disagrees
        # with; an omitted kwarg used to TypeError after the audio had
        # already been sent, killing the pump so she answered exactly once.
        audio = await engine.tts.synthesize(text, voice=_engine_voice(engine))
        pcm = normalize_pcm(wav_to_pcm(audio, TTS_SAMPLE_RATE)) if audio else b""
        if not pcm:
            log.warning("TTS returned no audio; reply dropped: %s", text[:60])
            return b""
        # Speech-per-character is the cheapest truncation detector there is.
        # F5-TTS can return a well-formed WAV that contains only the tail of
        # the sentence; the bytes look fine, so nothing else notices.
        secs = len(pcm) / 2 / TTS_SAMPLE_RATE
        if len(text) > 40 and secs < len(text) * 0.02:
            log.warning(
                "TTS output looks truncated: %.2fs for %d chars (%r...)",
                secs,
                len(text),
                text[:60],
            )
        return pcm

    async def publish(pcm: bytes, text: str = "", *, filler: bool = False) -> None:
        """Play already-rendered PCM on the agent's track.

        ``filler`` marks the short "working on it" acknowledgement, which must
        NOT count as taking a conversational turn: letting it call
        ``note_own_speech`` would open the roundtable follow-up window on a
        phrase that carries no content.
        """
        if not pcm:
            return
        speaking.set()
        speak_started["t"] = time.monotonic()
        if not filler:
            gate.note_own_speech()
        try:
            # 20 ms frames at 16-bit mono.
            step = int(TTS_SAMPLE_RATE * 0.02) * 2
            for i in range(0, len(pcm), step):
                chunk = pcm[i : i + step]
                if len(chunk) < step:
                    chunk = chunk + b"\x00" * (step - len(chunk))
                await source.capture_frame(
                    rtc.AudioFrame(chunk, TTS_SAMPLE_RATE, 1, len(chunk) // 2)
                )
            # capture_frame only fills a ~1s playout buffer, so returning from
            # the loop means "queued", NOT "spoken". Clearing `speaking` there
            # opened the VAD gate about a second before she stopped talking, so
            # her own tail leaked into the segmenter as the front of Chef's next
            # utterance. Wait for the buffer to actually drain.
            await source.wait_for_playout()
        except asyncio.CancelledError:
            # Barge-in, almost always. Stopping mid-sentence is the POINT, so
            # this is not an error, but it must be visible or "she cut out"
            # looks identical to a crash.
            #
            # Dropping the queued audio is the part that matters: without it
            # the buffered ~1s of a cancelled sentence still plays out, and the
            # NEXT reply's frames land behind it. That is the "weird two second
            # buzz before she talks" Chef heard: the tail of a killed sentence
            # smeared into the start of the next one.
            _drain(source)
            log.info("reply interrupted after %s", _preview(text))
            raise
        finally:
            speaking.clear()
            for det in bargers.values():
                det.reset()

    async def say(text: str, *, filler: bool = False) -> None:
        """Render and speak in one step. Used where there is nothing to overlap."""
        await publish(await synth(text), text, filler=filler)

    async def handle_utterance(speaker_id: str, pcm: bytes) -> None:
        transcript = (await engine.stt.transcribe(pcm) or "").strip()
        if not transcript:
            return
        if is_non_speech(transcript):
            # Whisper labels silence rather than returning nothing: "[BLANK_AUDIO]",
            # "(silence)", a lone "you" or "thank you" on room tone. Feeding those
            # to the LLM produced full replies to nobody ("sometimes silence says
            # more than words"), which burns a turn, talks over the operator, and
            # reads as her rambling. Observed live 2026-08-13.
            log.info("non-speech transcript ignored: %s", transcript[:40])
            return
        if dedup.is_duplicate(transcript):
            log.debug("duplicate transcript dropped: %s", transcript[:60])
            return
        if not gate.should_reply(speaker_id, transcript, mode=mode):
            log.debug("not addressed, staying quiet: %s: %s", speaker_id, transcript[:60])
            return
        log.info("%s: %s", speaker_id, transcript[:100])

        await _set_state(room, "thinking")

        # "I heard you, I'm working on it." Armed for every turn, but it only
        # SPEAKS if the reply has not arrived within FILLER_DELAY_S: a fast
        # answer cancels it before a word is synthesized, so quick exchanges
        # stay snappy and only real waits get covered.
        replied = asyncio.Event()
        spoke_filler = asyncio.Event()

        async def _filler_when_slow() -> None:
            if not FILLER_ALWAYS:
                try:
                    await asyncio.wait_for(replied.wait(), timeout=FILLER_DELAY_S)
                    return  # she beat the clock; say nothing
                except asyncio.TimeoutError:
                    pass
                if replied.is_set():
                    return
            text, bucket = pick_filler(transcript)
            spoke_filler.set()
            log.info("filler (%s): %s", bucket, text)
            await _set_state(room, "thinking", text)
            await say(text, filler=True)

            # Keep reassuring on a genuinely long turn. One "give me a sec"
            # covers a few seconds; a worship narration takes 30s+ to generate
            # and minutes to render, and Chef asked for exactly this after
            # sitting through that silence. Stops the moment the reply lands.
            while not replied.is_set():
                try:
                    await asyncio.wait_for(replied.wait(), timeout=FILLER_REPEAT_S)
                    return
                except asyncio.TimeoutError:
                    pass
                if replied.is_set():
                    return
                nudge = pick_waiting_filler()
                log.info("filler (waiting): %s", nudge)
                await say(nudge, filler=True)

        filler_task = asyncio.create_task(_filler_when_slow()) if FILLER_ENABLED else None

        try:
            reply = await run_turn(
                engine,
                history,
                transcript,
                mode=engine_mode(mode),
                speaker_id=speaker_id,
                is_operator=is_chef_identity(speaker_id),
            )
        finally:
            replied.set()
        gate.note_reply_to(speaker_id)
        # Log what she SAYS, not just what she heard. Without this half, a
        # truncated or empty reply is indistinguishable from a TTS fault: the
        # 2026-08-13 F5 cutover produced 0.2s of audio out of a 9s turn and
        # there was no way to tell whether the LLM or the synth had dropped it.
        log.info("reply: %s", (reply or "").strip()[:160] or "(empty)")

        # Start rendering the reply NOW, while the filler is still being
        # spoken, instead of after it. The filler exists to cover the wait, so
        # letting it also POSTPONE the answer defeats half its purpose: on a
        # measured turn the reply was ready at t+6.3s when the TTS could have
        # run inside the 2.5s the filler was already playing.
        t0 = time.monotonic()
        # Long replies are spoken in sentence-aligned chunks. Rendering a whole
        # narration before saying a word is minutes of silence, and past the TTS
        # timeout it is NO word at all: a 3400-char worship story rendered fine
        # and then died on the wire, logging "reply ready in 60.02s (0.0s of
        # audio)". Chunking also lets chunk N+1 render while chunk N plays.
        chunks = split_for_speech(reply or "")
        synth_task = asyncio.create_task(synth(chunks[0])) if chunks else None
        try:
            if filler_task is not None:
                if spoke_filler.is_set():
                    # Already talking. Let it land rather than clipping her
                    # mid-word, then the real reply follows on the same track.
                    try:
                        await filler_task
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - never eat the reply
                        log.warning("filler failed (%r)", exc)
                else:
                    filler_task.cancel()
            if synth_task is None:
                return
            await _set_state(room, "speaking", reply or "")
            spoken = 0
            for i, chunk in enumerate(chunks):
                pcm = await synth_task
                # Kick off the NEXT chunk before playing this one, so synthesis
                # overlaps playback instead of queueing behind it.
                synth_task = (
                    asyncio.create_task(synth(chunks[i + 1])) if i + 1 < len(chunks) else None
                )
                if i == 0:
                    log.info(
                        "first audio in %.2fs (%d chunk%s, %d chars)",
                        time.monotonic() - t0,
                        len(chunks),
                        "" if len(chunks) == 1 else "s",
                        len(reply or ""),
                    )
                spoken += len(pcm)
                await publish(pcm, chunk)
        except BaseException:
            # Includes barge-in cancelling the turn: a synth left running would
            # hold a TTS connection and then publish into a call that moved on.
            if synth_task is not None:
                synth_task.cancel()
            raise
        log.info(
            "reply done in %.2fs (%.1fs of audio)",
            time.monotonic() - t0,
            spoken / 2 / TTS_SAMPLE_RATE,
        )
        await _set_state(room, "listening")

    async def pump(stream, speaker_id: str) -> None:
        """Drain one participant's audio into their VAD segmenter."""
        seg = segmenters.setdefault(speaker_id, VADSegmenter())
        # Level telemetry. Without this, "she cannot hear me" is unfalsifiable:
        # the agent subscribes, receives every frame, and silently discards them
        # because they sit under the VAD gate. Log the observed peak against the
        # configured threshold so the gap is visible instead of guessed at, and
        # emit it on the data channel so a client can show a live level.
        peak = 0.0
        last_report = 0.0
        try:
            async for ev in stream:
                frame = getattr(ev, "frame", ev)
                pcm = bytes(frame.data)
                lvl = rms16(pcm)
                peak = max(peak, lvl)
                now = time.monotonic()
                if now - last_report >= 2.0:
                    log.info(
                        "hearing %s: peak_rms=%.0f gate=%d %s",
                        speaker_id,
                        peak,
                        RMS_VOICE_THRESHOLD,
                        "OPEN" if peak >= RMS_VOICE_THRESHOLD else "below gate (silent to VAD)",
                    )
                    await _emit_level(room, speaker_id, peak)
                    peak, last_report = 0.0, now
                # Barge-in: while she is speaking, sustained voice from the peer
                # cancels her reply. Only meaningful because the turn now runs as
                # a task and this loop keeps draining frames underneath it.
                if speaking.is_set() and (now - speak_started["t"]) * 1000 >= BARGE_IN_GRACE_MS:
                    det = bargers.setdefault(speaker_id, BargeInDetector())
                    if det.push(pcm):
                        t = turn["task"]
                        if t is not None and not t.done():
                            log.info("barge-in from %s: cancelling her turn", speaker_id)
                            t.cancel()
                elif speaker_id in bargers:
                    bargers[speaker_id].reset()
                # While we speak, keep feeding the segmenter (barge-in) but do
                # not let our own playback close an utterance.
                utt = seg.push(pcm, gated=speaking.is_set())
                if utt:
                    prev = turn["task"]
                    if prev is not None and not prev.done():
                        # She is still mid-turn and was not interrupted. Dropping
                        # is deliberate: awaiting here is what used to stall this
                        # loop, and stalling it is what made barge-in impossible.
                        log.info("turn in flight; dropping utterance from %s", speaker_id)
                    else:
                        turn["task"] = asyncio.create_task(handle_utterance(speaker_id, utt))
        except asyncio.CancelledError:
            raise
        except Exception:
            # WARNING, not debug. A bug in the turn used to end the pump silently
            # and present as "she hears me but never answers"; an AttributeError
            # on a mis-named dedup call hid here for a whole debugging session.
            log.warning("audio pump for %s ended on error", speaker_id, exc_info=True)

    tasks: list[asyncio.Task] = []

    pumped: set[str] = set()

    @room.on("track_subscribed")
    def _on_track(track_, publication, participant):  # noqa: ANN001
        if getattr(track_, "kind", None) != rtc.TrackKind.KIND_AUDIO:
            return
        who = getattr(participant, "identity", "") or "peer"
        # One pump per track. track_subscribed can fire more than once for the
        # same publication (renegotiation, a client republishing on reload), and
        # a second pump feeds the SAME per-speaker VADSegmenter interleaved
        # frames, so utterances never segment and she goes silent while the
        # level meter happily shows the gate open. Observed live: every level
        # line logged twice, zero transcripts.
        # Key on the PARTICIPANT, not the track sid. A client that republishes
        # (page reload, device switch) can leave a stale audio track alongside
        # the live one, so the sids differ while both carry the same speaker.
        # Two pumps then interleave frames into that speaker's single
        # VADSegmenter and no utterance ever segments: she hears you continuously
        # and never decides you finished a sentence. Observed live as every level
        # line printed twice with zero transcripts.
        if who in pumped:
            log.info(
                "ignoring extra audio track for %s (sid=%s); already pumping",
                who,
                getattr(publication, "sid", "?"),
            )
            return
        pumped.add(who)
        log.info("subscribed to audio from %s (sid=%s)", who, getattr(publication, "sid", "?"))
        stream = rtc.AudioStream(track_, sample_rate=STT_SAMPLE_RATE, num_channels=1)
        tasks.append(asyncio.create_task(pump(stream, who)))

    @room.on("track_unsubscribed")
    def _on_untrack(track_, publication, participant):  # noqa: ANN001
        if getattr(track_, "kind", None) != rtc.TrackKind.KIND_AUDIO:
            return
        who = getattr(participant, "identity", "") or "peer"
        pumped.discard(who)
        segmenters.pop(who, None)  # fresh VAD state on the next publish

    closed = asyncio.Event()

    @room.on("disconnected")
    def _on_disconnected(*_a):  # noqa: ANN001
        closed.set()

    @room.on("participant_disconnected")
    def _on_participant_left(participant):  # noqa: ANN001
        """Release a departed participant's per-speaker state. Does NOT hang up.

        Hanging up does not disconnect the agent (the SFU keeps it in the room
        with its track published), which is why a session had to learn to end
        itself at all: without it `closed` never fired, _ACTIVE_ROOMS never
        released the per-pair room, and the NEXT call was answered by the stale
        session. But ending the call HERE is wrong, and cut Chef off mid-call on
        2026-08-13:

            06:10:51  voice session live
            06:10:58  left and the room is empty; ending the session
            06:10:59  hearing lumina@chef...: peak_rms=37   <- still there

        A client that reconnects (network blip, page reload, handoff) rejoins
        with the SAME identity, and the old participant's disconnect event can
        arrive AFTER the replacement has joined. Counting "who else is here"
        while excluding that identity therefore reports an empty room for the
        one person who is still in it.

        So this only cleans up state, and the empty-room watchdog decides when
        the call is over. It samples real room membership repeatedly over
        SESSION_EMPTY_TIMEOUT_S, so a momentary gap cannot end a live call,
        which is the property a hangup signal needs and a single edge lacks.
        """
        who = getattr(participant, "identity", "") or "peer"
        pumped.discard(who)
        segmenters.pop(who, None)
        bargers.pop(who, None)
        log.info("%s left; letting the watchdog confirm before ending", who)

    await room.connect(url, token)
    await room.local_participant.publish_track(track)

    # A face on the call. Published after the audio track so a slow or missing
    # image can never delay the leg that actually matters.
    if AVATAR_ENABLED:
        avatar = load_avatar_rgba()
        if avatar is not None:
            av_w, av_h, av_rgba = avatar
            vsource = rtc.VideoSource(av_w, av_h)
            vtrack = rtc.LocalVideoTrack.create_video_track(f"{agent_name}-avatar", vsource)
            await room.local_participant.publish_track(
                vtrack, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
            )

            async def _pump_avatar() -> None:
                """Resend the still forever. WebRTC reads a stalled track as
                frozen, so a one-shot frame shows as a broken tile."""
                period = 1.0 / max(AVATAR_FPS, 0.5)
                frame = rtc.VideoFrame(av_w, av_h, rtc.VideoBufferType.RGBA, av_rgba)
                while not closed.is_set():
                    try:
                        vsource.capture_frame(frame)
                    except Exception as exc:  # noqa: BLE001 - video is cosmetic
                        log.debug("avatar frame dropped (%r)", exc)
                        return
                    await asyncio.sleep(period)

            tasks.append(asyncio.create_task(_pump_avatar()))
            log.info(
                "avatar published: %dx%d @%.1ffps from %s", av_w, av_h, AVATAR_FPS, AVATAR_IMAGE
            )

    # Her hands. Attached AFTER connect so a slow MCP spawn never delays the
    # join (a caller hearing nothing while 10 servers boot is the same bug as
    # publishing silence), and inside the session so the stdio processes live
    # and die with the call rather than with the answerer process.
    mcp = None
    registry = getattr(engine, "registry", None)
    if registry is not None:
        try:
            from skchat.voice_engine.builtin_tools import attach_mcp_tools  # noqa: PLC0415

            mcp = await attach_mcp_tools(registry)
        except Exception:  # noqa: BLE001 - never fail a call over tools
            log.warning("attaching MCP tools failed; continuing with built-ins", exc_info=True)

    log.info(
        "voice session live: room=%s agent=%s mode=%s peer=%s tools=%d",
        room_name,
        agent_name,
        mode,
        peer_fqid or "?",
        len(registry) if registry is not None else 0,
    )
    await _set_state(room, "listening")

    async def _empty_room_watchdog() -> None:
        """Backstop: end a session nobody is in.

        participant_disconnected is the clean signal, but it does not fire when
        a peer drops off the network rather than hanging up, and it cannot fire
        for a caller who never arrives. Without this, either case leaves an
        immortal session holding the per-pair room against every future call.
        Grace on entry so a session that starts before the caller finishes
        joining is not killed at birth.
        """
        empty_since: float | None = None
        while not closed.is_set():
            await asyncio.sleep(5)
            n = len(getattr(room, "remote_participants", {}) or {})
            if n:
                empty_since = None
                continue
            now = time.monotonic()
            if empty_since is None:
                empty_since = now
            elif now - empty_since >= SESSION_EMPTY_TIMEOUT_S:
                log.info(
                    "no participants for %.0fs; ending the session",
                    now - empty_since,
                )
                closed.set()
                return

    tasks.append(asyncio.create_task(_empty_room_watchdog()))
    try:
        await closed.wait()
    finally:
        t = turn["task"]
        if t is not None and not t.done():
            t.cancel()
        for t in tasks:
            t.cancel()
        if mcp is not None:
            try:
                await mcp.aclose_all()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                log.debug("MCP teardown failed", exc_info=True)
        try:
            await room.disconnect()
        except Exception:  # pragma: no cover - already gone
            pass
        log.info("voice session ended: room=%s", room_name)


async def run_agent(
    room_name: str | None = None,
    *,
    url: str | None = None,
    token: str | None = None,
    agent_name: str | None = None,
    peer_fqid: str | None = None,
) -> int:
    """Entry point: mint a token if one was not supplied, then run the session.

    Kept thin on purpose; the room model belongs to skchat's call routes, so a
    caller that already holds an answered invite (the answerer does) passes its
    room/url/token straight through and no minting happens here.
    """
    room_name = room_name or os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")
    agent_name = agent_name or os.getenv("LUMINA_IDENTITY", "lumina")
    if not (url and token):
        raise RuntimeError(
            "run_agent needs an SFU url + token; mint via POST /livekit/token "
            "(operator-gated) or pass the values from an answered invite."
        )
    await build_room_session(
        room_name, url=url, token=token, agent_name=agent_name, peer_fqid=peer_fqid
    )
    return 0
