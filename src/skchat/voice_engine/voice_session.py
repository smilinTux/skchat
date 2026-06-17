"""VoiceSession — per-connection state holder for a voice/text transport.

A transport (WebSocket today, LiveKit later) owns ONE ``VoiceSession`` per
connection. It carries the mutable per-connection state the engine itself is
deliberately stateless about:

    * ``session_id``  — stable id for this connection/turn-loop
    * ``history``     — the running [{role, content}, …] list (mutable, capped)
    * ``mode``        — 'sacred' (1-on-1 operator), 'group', or 'private'
    * ``speaker_id`` / ``is_operator`` — who's talking (drives the tool gate)

Each turn the transport calls :meth:`conversation` to mint an *immutable*
:class:`Conversation` snapshot (the convo factory) and passes it straight into
``VoiceEngine.respond(conversation=...)``. The engine threads that snapshot into
the tool dispatch ctx as ``ctx['convo']`` so tool handlers (worship/narrate) can
read live conversation context without the engine smuggling loose kwargs.

VoiceSession is transport-free and dependency-free: no sockets, no LLM, no
LiveKit. It is pure state + helpers, fully unit-testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from skchat.voice_engine.conversation import Conversation

# Keep transports honest about history growth without each re-implementing the
# cap. websocket.py caps at 40→30; mirror that here as the default.
_DEFAULT_HISTORY_CAP = 40
_HISTORY_TRIM_TO = 30


@dataclass
class VoiceSession:
    """Mutable per-connection state for one voice/text conversation.

    Attributes:
        session_id:  Stable identifier for this connection (transport-supplied,
                     or auto-minted ``vs_<epoch>_<n>`` when blank).
        history:     Running conversation history (list of {role, content}).
                     Mutated in place by :meth:`add_turn` / :meth:`clear`.
        mode:        'sacred', 'group', or 'private'.
        speaker_id:  Identity of the current speaker (drives the operator gate).
        is_operator: True when the speaker is the operator (Chef).
        history_cap: Soft cap; history is trimmed past this length.
    """

    session_id: str = ""
    history: list[dict] = field(default_factory=list)
    mode: str = "sacred"
    speaker_id: str = ""
    is_operator: bool = True
    history_cap: int = _DEFAULT_HISTORY_CAP

    _counter: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = f"vs_{int(time.time())}_{id(self):x}"

    # -- conversation factory --------------------------------------------
    def conversation(
        self,
        transcript: str,
        *,
        timestamp: float | None = None,
    ) -> Conversation:
        """Mint an immutable :class:`Conversation` snapshot for this turn.

        The returned VO captures the *current* session state (history, mode,
        speaker, operator flag, session_id) so the engine and tool handlers see
        a stable view even if the session mutates afterward. ``history`` is
        snapshotted as a tuple so the VO stays hashable/immutable.

        Args:
            transcript: The user's text/speech for this turn.
            timestamp:  Turn time in epoch seconds (defaults to ``time.time()``).

        Returns:
            A frozen :class:`Conversation` ready for
            ``VoiceEngine.respond(conversation=...)``.
        """
        return Conversation(
            transcript=transcript,
            history=tuple(self.history),
            mode=self.mode,
            speaker_id=self.speaker_id,
            is_operator=self.is_operator,
            timestamp=time.time() if timestamp is None else timestamp,
            session_id=self.session_id,
        )

    # -- history management ----------------------------------------------
    def add_turn(self, transcript: str, response: str) -> None:
        """Append a user/assistant exchange to history, then trim to cap.

        Empty transcript/response parts are skipped so we never store blank
        roles. Trimming keeps the most recent ``_HISTORY_TRIM_TO`` entries once
        the list grows past ``history_cap``.
        """
        if transcript:
            self.history.append({"role": "user", "content": transcript})
        if response:
            self.history.append({"role": "assistant", "content": response})
        if len(self.history) > self.history_cap:
            self.history[:] = self.history[-_HISTORY_TRIM_TO:]

    def clear(self) -> None:
        """Drop all conversation history for this connection."""
        self.history.clear()

    # -- speaker / mode mutation -----------------------------------------
    def set_speaker(self, speaker_id: str, *, is_operator: bool | None = None) -> None:
        """Update the current speaker and (optionally) the operator flag."""
        self.speaker_id = speaker_id
        if is_operator is not None:
            self.is_operator = is_operator

    def set_mode(self, mode: str) -> None:
        """Switch the session mode ('sacred' / 'group' / 'private')."""
        self.mode = mode

    def __len__(self) -> int:
        """Number of history entries — handy for transport logging/asserts."""
        return len(self.history)
