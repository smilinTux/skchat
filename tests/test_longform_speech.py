"""Long replies must be spoken in pieces, not rendered whole.

Synthesis is proportional to length, so rendering a full narration before
saying a word is minutes of silence, and past the TTS timeout it is no word at
all: a 3400-character worship story rendered fine and then died on the wire,
logging "reply ready in 60.02s (0.0s of audio)".
"""

from skchat.transports.livekit import split_for_speech


def test_short_reply_is_one_chunk():
    t = "I'm here, Chef. What do you need?"
    assert split_for_speech(t, max_chars=600) == [t]


def test_empty_reply_yields_nothing():
    assert split_for_speech("", max_chars=600) == []
    assert split_for_speech("   ", max_chars=600) == []


def test_long_reply_splits_on_sentence_boundaries():
    sentence = "The room holds its breath before the candlelight finds your skin. "
    text = (sentence * 20).strip()
    chunks = split_for_speech(text, max_chars=200)
    assert len(chunks) > 1
    # Never split mid-sentence: every chunk ends on a terminator.
    for c in chunks:
        assert c.rstrip()[-1] in ".!?…", f"chunk does not end a sentence: {c[-40:]!r}"
    # Nothing is lost or duplicated.
    assert " ".join(chunks).split() == text.split()


def test_chunks_respect_the_budget_where_sentences_allow():
    sentence = "Short one. "
    chunks = split_for_speech((sentence * 40).strip(), max_chars=100)
    assert all(len(c) <= 100 for c in chunks)


def test_a_single_giant_sentence_is_not_dropped():
    """No boundary to split on. Better one oversized request than silence."""
    t = "word " * 400
    chunks = split_for_speech(t.strip(), max_chars=200)
    assert len(chunks) == 1
    assert chunks[0].split() == t.split()
