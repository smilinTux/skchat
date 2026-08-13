"""The token cap is the only hard guarantee on reply length, and hitting it
ends her mid-word. Speech has no scrollbar, so a truncated tail is just a
broken sentence: trim back to the last completed one."""

import pytest

from skchat.voice_engine.llm import strip_formatting, trim_dangling_sentence


@pytest.mark.parametrize(
    "text,expected",
    [
        # Already ends cleanly: never touched.
        ("I'm here, Chef. What do you need?", "I'm here, Chef. What do you need?"),
        ("Yeah.", "Yeah."),
        ("Really?!", "Really?!"),
        ("", ""),
        # Cut off mid-word: trim back to the last finished sentence.
        (
            "I'm here, Chef. I was thinking about how the whole thing dep",
            "I'm here, Chef.",
        ),
        # Closing punctuation may follow the terminator.
        ('She said "go on." And then I wond', 'She said "go on."'),
    ],
)
def test_trim_dangling_sentence(text, expected):
    assert trim_dangling_sentence(text) == expected


def test_keeps_a_lone_unterminated_sentence():
    """One long unterminated thought has no boundary to fall back to. A
    dangling clause beats saying nothing at all."""
    t = "This is one long unterminated thought that never ends properly"
    assert trim_dangling_sentence(t) == t


def test_refuses_a_trim_that_would_leave_only_a_greeting():
    """Guard on what SURVIVES, not on how much is removed. An earlier version
    capped the trim at 40% of the reply, which rejected exactly the common
    case while accepting nothing useful."""
    t = "Hi. " + "b" * 60
    assert trim_dangling_sentence(t) == t


def test_strip_formatting_applies_the_trim_last():
    """It must see the text exactly as it will be spoken, i.e. after markdown
    and emoji are gone."""
    assert (
        strip_formatting("**I'm here, Chef.** I was mid-thought when it cu") == "I'm here, Chef."
    )
