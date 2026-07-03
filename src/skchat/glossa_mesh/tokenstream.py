"""SKGlossa L3 — token-stream codec (G2, spec §2 extension).

L3 sits ABOVE the L0/L1/L2 ladder (skcomms.glossa.codec). Where L0-L2 encode a
Message as ONE self-contained frame, L3 emits the Message as an ordered STREAM of
small typed tokens that a peer can consume incrementally (token-level gloss):

    INTENT · ARG* · REF* · TEXT* · END

Every token reconstructs part of the Message IR, so a receiver can begin glossing
before the whole frame has arrived (streaming), and the text slot may be split
across several TEXT chunks. The wire form is a CBOR list of ``[tag, value]``
tokens, so L3 is as compact as L1/L2 while remaining strictly additive: it is a
new tier number, gated behind tier negotiation, and never changes L0-L2.

Round-trip invariant: ``decode_l3(encode_l3(m)) == m`` for any Message, with or
without a codebook (the codebook only shortens the INTENT token).
"""

from __future__ import annotations

from typing import Iterable, Iterator

import cbor2

from skcomms.glossa.codebook import Codebook
from skcomms.glossa.message import Message

# L3 extends the skcomms ladder (L0=0, L1=1, L2=2). Kept here so skcomms stays
# unmodified; codec_ext re-exports it alongside the L0-L2 constants.
L3_TOKENSTREAM = 3

# Token tags (the token-stream vocabulary).
T_INTENT = 0   # value: int codebook code, or str intent
T_ARG = 1      # value: [key, val]
T_REF = 2      # value: ref
T_TEXT = 3     # value: str chunk (the text slot, possibly streamed in pieces)
T_END = 4      # value: None — terminates a well-formed stream

Token = list  # [tag, value]


def iter_tokens(
    m: Message,
    codebook: Codebook | None = None,
    *,
    text_chunk: int | None = None,
) -> Iterator[Token]:
    """Yield the token stream for a Message, in canonical order.

    The INTENT token carries the codebook code when the codebook knows the
    intent (denser), else the raw intent string. ``text_chunk`` optionally splits
    the text slot into fixed-size character chunks to exercise streaming; the
    default emits the whole text as a single TEXT token (still streaming-shaped).
    """
    code = codebook.code_for(m.intent) if codebook is not None else None
    yield [T_INTENT, code if code is not None else m.intent]
    for k, v in m.args.items():
        yield [T_ARG, [k, v]]
    for r in m.refs:
        yield [T_REF, r]
    if m.text:
        if text_chunk and text_chunk > 0:
            for i in range(0, len(m.text), text_chunk):
                yield [T_TEXT, m.text[i : i + text_chunk]]
        else:
            yield [T_TEXT, m.text]
    yield [T_END, None]


class TokenStreamDecoder:
    """Incremental L3 decoder: feed tokens with ``push`` (as they arrive) and read
    ``message`` once ``complete``. Reassembles intent/args/refs/text; TEXT chunks
    are concatenated in arrival order. This is the streaming half of the invariant:
    the same Message results whether the stream came in one frame or token-by-token.
    """

    def __init__(self, codebook: Codebook | None = None) -> None:
        self._codebook = codebook
        self._intent: str = ""
        self._args: dict = {}
        self._refs: list = []
        self._text_parts: list[str] = []
        self._intent_seen = False
        self.complete = False

    def push(self, token: Token) -> None:
        if self.complete:
            raise ValueError("push after END — token stream already terminated")
        if not isinstance(token, (list, tuple)) or len(token) != 2:
            raise ValueError(f"malformed L3 token (expected [tag, value]): {token!r}")
        tag, value = token
        if tag == T_INTENT:
            if isinstance(value, int):
                concept = (
                    self._codebook.concept_for(value)
                    if self._codebook is not None
                    else None
                )
                if concept is None:
                    raise ValueError(
                        f"unknown codebook code {value} — codebook version skew"
                    )
                self._intent = concept
            else:
                self._intent = value or ""
            self._intent_seen = True
        elif tag == T_ARG:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError(f"malformed ARG token: {value!r}")
            self._args[value[0]] = value[1]
        elif tag == T_REF:
            self._refs.append(value)
        elif tag == T_TEXT:
            self._text_parts.append(value)
        elif tag == T_END:
            if not self._intent_seen:
                raise ValueError("L3 token-stream ended before an INTENT token")
            self.complete = True
        else:
            raise ValueError(f"unknown L3 token tag {tag}")

    @property
    def message(self) -> Message:
        """The Message assembled so far. Prefer reading after ``complete`` is True;
        a partial stream still yields a best-effort Message (streaming preview)."""
        return Message(
            intent=self._intent,
            args=dict(self._args),
            refs=list(self._refs),
            text="".join(self._text_parts),
        )


def encode_l3(
    m: Message, codebook: Codebook | None = None, *, text_chunk: int | None = None
) -> bytes:
    """Encode a Message to an L3 token-stream frame (a CBOR list of tokens)."""
    return cbor2.dumps(list(iter_tokens(m, codebook, text_chunk=text_chunk)))


def decode_l3(raw: bytes, codebook: Codebook | None = None) -> Message:
    """Decode an L3 token-stream frame back to a Message. Raises ValueError on a
    malformed or unterminated (no END) stream — mirrors the L2 malformed-frame gate.
    """
    tokens = cbor2.loads(raw)
    if not isinstance(tokens, list):
        raise ValueError("malformed L3 frame — expected a CBOR list of tokens")
    dec = TokenStreamDecoder(codebook)
    for tok in tokens:
        dec.push(tok)
    if not dec.complete:
        raise ValueError("unterminated L3 token-stream — missing END token")
    return dec.message


def decode_stream(
    tokens: Iterable[Token], codebook: Codebook | None = None
) -> TokenStreamDecoder:
    """Drive a decoder from an iterable of tokens (the streaming entry point).
    Returns the decoder so callers can inspect partial state / ``complete``."""
    dec = TokenStreamDecoder(codebook)
    for tok in tokens:
        dec.push(tok)
    return dec
