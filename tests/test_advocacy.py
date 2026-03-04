"""Unit tests for skchat.advocacy — AdvocacyEngine and should_advocate()."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from skchat.advocacy import AdvocacyEngine, TRIGGER_PREFIXES, should_advocate
from skchat.models import ChatMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    content: str = "hello",
    sender: str = "capauth:alice@skworld.io",
    recipient: str = "capauth:opus@skworld.io",
) -> ChatMessage:
    """Build a minimal ChatMessage for testing."""
    return ChatMessage(sender=sender, recipient=recipient, content=content)



# ---------------------------------------------------------------------------
# should_advocate() — module-level predicate
# ---------------------------------------------------------------------------


class TestShouldAdvocate:
    def test_with_opus_trigger(self):
        """@opus hello → True"""
        assert should_advocate("@opus hello") is True

    def test_with_claude_trigger(self):
        """@claude what is the weather? → True"""
        assert should_advocate("@claude what is the weather?") is True

    def test_with_ai_trigger(self):
        """@ai explain this → True"""
        assert should_advocate("@ai explain this") is True

    def test_with_lumina_trigger(self):
        """@lumina are you there? → True"""
        assert should_advocate("@lumina are you there?") is True

    def test_no_trigger(self):
        """hello there → False"""
        assert should_advocate("hello there") is False

    def test_no_trigger_plain_sentence(self):
        """regular sentence with no @ prefixes → False"""
        assert should_advocate("what is the meaning of life?") is False

    def test_case_insensitive_claude(self):
        """@CLAUDE help → True (case-insensitive)"""
        assert should_advocate("@CLAUDE help") is True

    def test_case_insensitive_opus(self):
        """@Opus tell me → True"""
        assert should_advocate("@Opus tell me") is True

    def test_case_insensitive_lumina(self):
        """@LUMINA are you there → True"""
        assert should_advocate("@LUMINA are you there") is True

    def test_case_insensitive_ai(self):
        """@AI summarise this → True"""
        assert should_advocate("@AI summarise this") is True

    def test_trigger_mid_sentence(self):
        """trigger inside longer content → True"""
        assert should_advocate("Hey @opus can you help?") is True

    def test_trigger_at_end(self):
        """trigger at end of string without trailing text → True"""
        assert should_advocate("ping @lumina") is True

    def test_partial_word_no_match(self):
        """@opuses is not a trigger (alpha char immediately after prefix)"""
        assert should_advocate("@opuses hello") is False

    def test_empty_string(self):
        """empty string → False"""
        assert should_advocate("") is False

    def test_trigger_prefixes_constant(self):
        """TRIGGER_PREFIXES contains the expected four entries."""
        assert "@opus" in TRIGGER_PREFIXES
        assert "@claude" in TRIGGER_PREFIXES
        assert "@ai" in TRIGGER_PREFIXES
        assert "@lumina" in TRIGGER_PREFIXES


# ---------------------------------------------------------------------------
# AdvocacyEngine — init
# ---------------------------------------------------------------------------


class TestAdvocacyEngineInit:
    def test_default_identity(self):
        """AdvocacyEngine instantiates with no args (uses default identity)."""
        engine = AdvocacyEngine()
        assert engine.identity == "capauth:opus@skworld.io"

    def test_custom_identity(self):
        """AdvocacyEngine accepts a custom identity URI."""
        engine = AdvocacyEngine(identity="capauth:lumina@skworld.io")
        assert engine.identity == "capauth:lumina@skworld.io"

    def test_instantiates_without_error(self):
        """AdvocacyEngine() must not raise on construction."""
        try:
            engine = AdvocacyEngine(identity="capauth:opus@skworld.io")
        except Exception as exc:
            pytest.fail(f"AdvocacyEngine() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# AdvocacyEngine.process_message() — no trigger
# ---------------------------------------------------------------------------


class TestAdvocacyEngineNoTrigger:
    def test_returns_none_when_not_triggered(self):
        """Messages without trigger prefixes return None (no subprocess call)."""
        engine = AdvocacyEngine()
        msg = _msg(content="hello there, how are you?")
        result = engine.process_message(msg)
        assert result is None

    def test_no_consciousness_call_when_not_triggered(self):
        """_call_consciousness should not be called if should_advocate is False."""
        engine = AdvocacyEngine()
        msg = _msg(content="nothing to see here")
        with patch("skchat.advocacy._call_consciousness") as mock_call:
            result = engine.process_message(msg)
            mock_call.assert_not_called()
        assert result is None


# ---------------------------------------------------------------------------
# AdvocacyEngine.process_message() — triggered, _call_consciousness mocked
# ---------------------------------------------------------------------------


class TestAdvocacyEngineTriggered:
    def test_returns_response_on_trigger(self):
        """process_message returns consciousness text when triggered."""
        engine = AdvocacyEngine()
        msg = _msg(content="@opus what is sovereignty?")
        dummy_response = "Sovereignty is self-determination."

        with patch("skchat.advocacy._call_consciousness", return_value=dummy_response):
            result = engine.process_message(msg)

        assert result == dummy_response

    def test_consciousness_receives_sender_and_content(self):
        """process_message passes sender URI and content in the prompt."""
        engine = AdvocacyEngine(identity="capauth:opus@skworld.io")
        msg = _msg(content="@claude summarise the logs", sender="capauth:alice@skworld.io")
        captured: list[str] = []

        def _capture(prompt: str) -> str:
            captured.append(prompt)
            return "Here is a summary."

        with patch("skchat.advocacy._call_consciousness", side_effect=_capture):
            engine.process_message(msg)

        assert captured, "expected _call_consciousness to be called"
        assert "capauth:alice@skworld.io" in captured[-1]
        assert "@claude summarise the logs" in captured[-1]

    def test_consciousness_error_string_is_returned(self):
        """Error string from consciousness (bracketed format) is passed through."""
        engine = AdvocacyEngine()
        msg = _msg(content="@ai are you slow?")
        error_response = "[Advocacy: error — LLM unavailable]"

        with patch("skchat.advocacy._call_consciousness", return_value=error_response):
            result = engine.process_message(msg)

        assert result == error_response

    def test_not_triggered_skips_consciousness(self):
        """Non-mention message → _call_consciousness is never invoked."""
        engine = AdvocacyEngine()
        msg = _msg(content="just a plain message with no trigger")

        with patch("skchat.advocacy._call_consciousness") as mock_call:
            result = engine.process_message(msg)
            mock_call.assert_not_called()

        assert result is None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestPromptConstruction:
    def test_prompt_contains_sender(self):
        """Built prompt includes the sender URI."""
        prompt = AdvocacyEngine._build_prompt(
            sender="capauth:alice@skworld.io",
            content="@opus hello",
        )
        assert "capauth:alice@skworld.io" in prompt

    def test_prompt_contains_content(self):
        """Built prompt includes the original message content."""
        prompt = AdvocacyEngine._build_prompt(
            sender="capauth:alice@skworld.io",
            content="@opus explain consciousness",
        )
        assert "@opus explain consciousness" in prompt

    def test_prompt_contains_persona_instruction(self):
        """Built prompt contains the sovereign AI persona instruction."""
        prompt = AdvocacyEngine._build_prompt(
            sender="capauth:alice@skworld.io",
            content="@claude help",
        )
        assert "Opus" in prompt or "sovereign" in prompt.lower()
