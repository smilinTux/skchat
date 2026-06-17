"""Conversation value-object — an immutable snapshot of one voice turn.

Threaded into `VoiceEngine.respond(..., conversation=...)` and exposed to tool
handlers via the dispatch ctx (`ctx['convo']`) so tools (e.g. worship/narrate)
can read live conversation context — who is speaking, the mode, the running
history — without the engine smuggling loose kwargs.

Immutable (frozen dataclass): the engine never mutates a turn in place; a new
Conversation is built per turn. `to_dict()` gives a JSON-friendly snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Conversation:
    """An immutable snapshot of a single conversational turn.

    Attributes:
        transcript:  The user's spoken/typed text for this turn.
        response:    The reply produced for this turn ("" before the LLM runs).
        history:     Prior conversation history (list of {role, content} dicts).
        mode:        'sacred', 'group', or 'private'.
        speaker_id:  Identity of the speaker (used by the operator gate).
        is_operator: True when the speaker is the operator (Chef).
        timestamp:   Unix epoch seconds for the turn (caller-supplied).
        session_id:  Transport/session identifier, if any.
    """

    transcript: str
    response: str = ""
    history: tuple[dict, ...] = field(default_factory=tuple)
    mode: str = "sacred"
    speaker_id: str = ""
    is_operator: bool = True
    timestamp: float = 0.0
    session_id: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-friendly snapshot of this turn.

        `history` is normalized to a list so the result round-trips through
        ``json.dumps`` cleanly.
        """
        d = asdict(self)
        d["history"] = list(self.history)
        return d
