"""Unit tests for the LiveKit transport's non-network logic.

Covers the three behaviours the Phase-3 re-home must preserve without a live
LiveKit room: addressing-cap, VAD gating, and roundtable ordering (plus the
barge-in dwell detector and transcript de-dup guards). All use an injectable
clock so they are deterministic and fast.
"""

from __future__ import annotations

import struct

from skchat.transports.livekit import (
    AddressingGate,
    BargeInDetector,
    TranscriptDedup,
    VADSegmenter,
    is_chef_identity,
    mode_ceiling,
    rms16,
)

FRAME_MS = 20
SR = 16000
SAMPLES = SR * FRAME_MS // 1000  # 320 samples / 20 ms frame


def frame(amplitude: int) -> bytes:
    """A 20 ms int16-LE mono frame of constant amplitude (RMS == |amplitude|)."""
    return struct.pack(f"<{SAMPLES}h", *([amplitude] * SAMPLES))


VOICED = frame(3000)   # well above 1200 speech gate and 2000 barge-in gate
QUIET = frame(0)       # silence


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ─── rms16 / helpers ────────────────────────────────────────────────────────
def test_rms16_matches_amplitude():
    assert abs(rms16(frame(3000)) - 3000) < 1.0
    assert rms16(QUIET) == 0.0
    assert rms16(b"") == 0.0


def test_is_chef_identity_prefixes():
    assert is_chef_identity("chef-laptop", ("chef",))
    assert is_chef_identity("chef-phone", ("chef",))
    assert not is_chef_identity("opus", ("chef",))
    assert not is_chef_identity("", ("chef",))


def test_mode_ceiling():
    assert mode_ceiling("lumina-and-chef") == "sacred"
    assert mode_ceiling("random-room") == "group"
    assert mode_ceiling("") == "group"


# ─── VAD gating ─────────────────────────────────────────────────────────────
def _feed(seg: VADSegmenter, clk: FakeClock, frames, *, gated=False):
    """Feed frames advancing the clock one frame_ms each; return first flush."""
    out = None
    for fr in frames:
        r = seg.push(fr, gated=gated)
        clk.advance(FRAME_MS / 1000.0)
        if r is not None:
            out = r
    return out


def test_vad_silence_never_flushes():
    clk = FakeClock()
    seg = VADSegmenter(clock=clk, frame_ms=FRAME_MS)
    assert _feed(seg, clk, [QUIET] * 100) is None


def test_vad_flushes_after_silence_hangover():
    clk = FakeClock()
    seg = VADSegmenter(clock=clk, frame_ms=FRAME_MS)
    # 40 voiced frames = 800 ms speech (> 600 ms min), then enough silence to
    # cross the 800 ms hangover.
    voiced = [VOICED] * 40
    silence = [QUIET] * 45  # 900 ms > 800 ms hangover
    pcm = _feed(seg, clk, voiced + silence)
    assert pcm is not None
    # Utterance contains the voiced frames (+ trailing silence up to flush).
    assert len(pcm) >= 40 * len(VOICED)


def test_vad_short_blip_plus_hangover_still_emits():
    # Faithful quirk of the ported state machine: min_utterance_ms is measured
    # start->flush, and the flush only happens after the 800 ms silence
    # hangover — so a short voiced blip followed by full silence exceeds
    # min_utterance_ms (200 ms voice + 800 ms hangover = 1000 ms > 600) and IS
    # emitted. The min gate only drops utterances on the max-flush path (below).
    clk = FakeClock()
    seg = VADSegmenter(clock=clk, frame_ms=FRAME_MS)
    pcm = _feed(seg, clk, [VOICED] * 10 + [QUIET] * 45)
    assert pcm is not None


def test_vad_force_flush_at_max_length():
    clk = FakeClock()
    seg = VADSegmenter(clock=clk, frame_ms=FRAME_MS, min_utterance_ms=100, max_utterance_ms=400)
    # Continuous voice past the (shrunk) 400 ms max → force flush, no silence.
    pcm = _feed(seg, clk, [VOICED] * 30)
    assert pcm is not None


def test_vad_max_flush_below_min_is_dropped():
    # When a max-flush segment is shorter than min_utterance_ms it is dropped.
    clk = FakeClock()
    seg = VADSegmenter(clock=clk, frame_ms=FRAME_MS, min_utterance_ms=600, max_utterance_ms=400)
    assert _feed(seg, clk, [VOICED] * 30) is None


def test_vad_gated_ignores_mic():
    clk = FakeClock()
    seg = VADSegmenter(clock=clk, frame_ms=FRAME_MS)
    # While Lumina is speaking (gated) even loud frames never start an utterance.
    assert _feed(seg, clk, [VOICED] * 60, gated=True) is None


# ─── Barge-in ───────────────────────────────────────────────────────────────
def test_barge_in_fires_after_dwell():
    det = BargeInDetector(dwell_ms=300, frame_ms=FRAME_MS, enabled=True)
    fired = [det.push(VOICED) for _ in range(15)]  # 15*20ms = 300ms
    assert True in fired
    # Fires exactly when accumulated voiced time hits the dwell (15th frame).
    assert fired.index(True) == 14


def test_barge_in_decays_on_silence():
    det = BargeInDetector(dwell_ms=300, frame_ms=FRAME_MS, enabled=True)
    # Alternate voiced/quiet so accumulated voiced time never reaches dwell.
    assert not any(det.push(VOICED if i % 2 == 0 else QUIET) for i in range(40))


def test_barge_in_disabled():
    det = BargeInDetector(dwell_ms=20, frame_ms=FRAME_MS, enabled=False)
    assert not any(det.push(VOICED) for _ in range(10))


# ─── Addressing + roundtable ────────────────────────────────────────────────
def make_gate(clk, **kw):
    return AddressingGate(
        identity="lumina",
        display_name="Lumina",
        chef_prefixes=("chef",),
        clock=clk,
        **kw,
    )


def test_sacred_chef_is_always_addressed():
    clk = FakeClock()
    g = make_gate(clk)
    assert g.is_addressed("chef-laptop", "what's the weather", mode="sacred")


def test_named_other_agent_stays_out():
    clk = FakeClock()
    g = make_gate(clk)
    # Directed at opus by name, Lumina not named → not for her.
    assert not g.is_addressed(
        "chef-laptop", "opus what do you think", mode="group", other_agents=("opus",)
    )


def test_named_me_engages_even_in_group():
    clk = FakeClock()
    g = make_gate(clk)
    assert g.is_addressed(
        "guest-1", "hey lumina can you help", mode="group", other_agents=("opus",)
    )


def test_follow_up_window_rolls_unnamed_turns():
    clk = FakeClock()
    g = make_gate(clk, follow_up_window_s=60)
    # First turn names her → engaged.
    assert g.is_addressed("chef-laptop", "lumina hi", mode="group")
    clk.advance(10)
    # Un-named follow-up within the window still counts.
    assert g.is_addressed("chef-laptop", "and the time?", mode="group")
    clk.advance(120)
    # After the window a bare un-named turn (no wake word, other agent present)
    # no longer engages.
    assert not g.is_addressed("chef-laptop", "and the time?", mode="group",
                              other_agents=("opus",))


def test_generic_wakeword_only_when_alone():
    clk = FakeClock()
    g = make_gate(clk)
    # 'what do you think' is a generic trigger — engages when no other agent.
    assert g.is_addressed("guest-1", "what do you think", mode="group")
    clk.advance(999)
    g2 = make_gate(FakeClock())
    # …but NOT when another agent is present (avoid both bots answering).
    assert not g2.is_addressed(
        "guest-1", "what do you think", mode="group", other_agents=("opus",)
    )


def test_roundtable_broadcast_window():
    clk = FakeClock()
    g = make_gate(clk, follow_up_window_s=60)
    g.note_own_speech()  # Lumina just spoke → broadcast window open
    clk.advance(5)
    # A peer AGENT's un-named turn within the window drives the roundtable.
    assert g.is_addressed("opus", "I agree with that", mode="group",
                          other_agents=("opus",))
    # …but Chef's un-directed turn does NOT get grabbed via broadcast window.
    g2 = make_gate(clk, follow_up_window_s=60)
    g2.note_own_speech()
    assert not g2.is_addressed("chef-laptop", "hmm interesting", mode="group",
                               other_agents=("opus",))


def test_agent_turn_cap_damps_pingpong():
    clk = FakeClock()
    g = make_gate(clk, agent_turn_cap=3, follow_up_window_s=600)
    g.note_own_speech()
    # Peer agent keeps replying within the broadcast/engaged window.
    replies = [
        g.should_reply("opus", "and another thing", mode="group", other_agents=("opus",))
        for _ in range(6)
    ]
    # First `cap` peer-agent turns allowed, then damped to silence.
    assert replies[:3] == [True, True, True]
    assert replies[3:] == [False, False, False]


def test_human_turn_resets_streak():
    clk = FakeClock()
    g = make_gate(clk, agent_turn_cap=2, follow_up_window_s=600)
    g.note_own_speech()
    assert g.should_reply("opus", "point one", mode="group", other_agents=("opus",))
    assert g.should_reply("opus", "point two", mode="group", other_agents=("opus",))
    assert not g.should_reply("opus", "point three", mode="group", other_agents=("opus",))
    # A human speaks → streak resets, agents may participate again.
    g.should_reply("chef-laptop", "ok go on", mode="sacred")
    g.note_own_speech()
    assert g.should_reply("opus", "resumed", mode="group", other_agents=("opus",))


# ─── Transcript de-dup ──────────────────────────────────────────────────────
def test_dedup_drops_repeat_within_window():
    clk = FakeClock()
    d = TranscriptDedup(window_s=3.0, clock=clk)
    assert not d.is_duplicate("hello there")
    assert d.is_duplicate("Hello there.")  # normalized match
    clk.advance(5)
    assert not d.is_duplicate("hello there")  # window expired


def test_whisper_repetition_filter():
    assert TranscriptDedup.is_whisper_repetition("If If If If If if if if If")
    assert TranscriptDedup.is_whisper_repetition("Bye. Bye. Bye. Bye. Bye. Bye.")
    assert not TranscriptDedup.is_whisper_repetition("this is a normal sentence here")
