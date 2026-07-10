"""Unit tests for skchat.advocacy — AdvocacyEngine and should_advocate()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skchat.advocacy import TRIGGER_PREFIXES, AdvocacyEngine, _token_match, should_advocate
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


# ---------------------------------------------------------------------------
# QA additions — token-boundary edges, inject_context, memory-context wiring
# ---------------------------------------------------------------------------


class TestShouldAdvocateBoundaries:
    def test_trigger_followed_by_comma(self):
        """@opus, ... is a valid trigger (comma is a boundary)."""
        assert should_advocate("@opus, are you there") is True

    def test_trigger_followed_by_colon(self):
        """@lumina: ... is a valid trigger (colon is a boundary)."""
        assert should_advocate("@lumina: hello") is True

    def test_trigger_followed_by_digit_is_boundary(self):
        """@ai2 — a digit is non-alpha, so this still triggers per current rules."""
        assert should_advocate("@ai2 go") is True

    def test_multiple_triggers_one_match_enough(self):
        assert should_advocate("hey @opus and @lumina") is True

    def test_email_address_does_not_falsely_trigger(self):
        """An @-mention embedded in a word ("@claudette") must not trigger."""
        assert should_advocate("contact claudette@x.com") is False


class TestTokenMatchLeftBoundary:
    """Regression tests for defect #3 (wave-a/03-bughunt-responder.md):

    _token_match() only checked the boundary AFTER the prefix, never BEFORE
    the "@" — so an email address like "sam.opus@opus-mail.com" false-
    triggered "@opus". Both sides must now be real token boundaries.
    """

    # -- emails must NOT match -------------------------------------------------

    def test_email_local_part_suffix_does_not_match(self):
        """"sam.opus@opus-mail.com" contains "@opus" but is preceded by a word
        char ('s' of "opus") — must not match."""
        assert _token_match("please cc sam.opus@opus-mail.com on the reply", "@opus") is False

    def test_should_advocate_false_for_opus_email(self):
        assert should_advocate("please cc sam.opus@opus-mail.com on the reply") is False

    def test_lumina_domain_email_does_not_match(self):
        """"danielle@lumina-imports.com" contains "@lumina" preceded by 'e' —
        must not match."""
        assert _token_match(
            "reach danielle at danielle@lumina-imports.com about the invoice", "@lumina"
        ) is False

    def test_should_advocate_false_for_lumina_email(self):
        assert should_advocate(
            "reach danielle at danielle@lumina-imports.com about the invoice"
        ) is False

    # -- real mentions must still match -----------------------------------------

    def test_mention_at_start_of_string_matches(self):
        assert _token_match("@opus are you there", "@opus") is True

    def test_mention_mid_sentence_after_space_matches(self):
        assert _token_match("hey @opus can you help", "@opus") is True

    def test_mention_after_punctuation_matches(self):
        """A mention immediately after punctuation (e.g. a leading paren or
        comma) is still a real mention — non-alnum before "@" is a boundary."""
        assert _token_match("(cc: @opus) please respond", "@opus") is True
        assert _token_match("chef,@lumina you around?", "@lumina") is True

    def test_lumina_real_mention_still_matches(self):
        assert should_advocate("hey @lumina, you around?") is True

    def test_opus_real_mention_still_matches(self):
        assert should_advocate("@opus what is sovereignty?") is True


class TestInjectContext:
    def test_inject_context_updates_identity(self):
        engine = AdvocacyEngine(identity="capauth:opus@skworld.io")
        engine.inject_context("capauth:lumina@skworld.io")
        assert engine.identity == "capauth:lumina@skworld.io"


class TestMemoryContext:
    """_get_memory_context shells out to skcapstone-mcp via subprocess.run."""

    def _mcp_result(self, returncode=0, stdout=""):
        return MagicMock(returncode=returncode, stdout=stdout)

    def test_memory_context_parses_snippets(self):
        engine = AdvocacyEngine()
        import json as _json

        mem_blocks = _json.dumps(
            [{"content": "Chef likes brevity"}, {"text": "release v1.2 shipped"}]
        )
        rpc = _json.dumps({"result": {"content": [{"type": "text", "text": mem_blocks}]}})

        with patch("skchat.advocacy.subprocess.run", return_value=self._mcp_result(0, rpc)):
            ctx = engine._get_memory_context("release status")

        assert "Relevant context:" in ctx
        assert "Chef likes brevity" in ctx
        assert "release v1.2 shipped" in ctx

    def test_memory_context_returns_empty_on_nonzero_exit(self):
        engine = AdvocacyEngine()
        with patch(
            "skchat.advocacy.subprocess.run",
            return_value=self._mcp_result(returncode=1, stdout=""),
        ):
            assert engine._get_memory_context("q") == ""

    def test_memory_context_returns_empty_on_subprocess_error(self):
        engine = AdvocacyEngine()
        with patch(
            "skchat.advocacy.subprocess.run",
            side_effect=FileNotFoundError("skcapstone-mcp not found"),
        ):
            assert engine._get_memory_context("q") == ""

    def test_memory_context_empty_when_no_memories(self):
        engine = AdvocacyEngine()
        import json as _json

        rpc = _json.dumps({"result": {"content": [{"type": "text", "text": "[]"}]}})
        with patch("skchat.advocacy.subprocess.run", return_value=self._mcp_result(0, rpc)):
            assert engine._get_memory_context("q") == ""

    def test_process_message_prepends_memory_context(self):
        """When memory context is found, it is woven into the prompt before send."""
        engine = AdvocacyEngine()
        msg = _msg(content="@opus what shipped?")
        captured: list[str] = []

        with patch.object(
            engine, "_get_memory_context", return_value="Relevant context:\n- v1.2 shipped"
        ):
            with patch(
                "skchat.advocacy._call_consciousness",
                side_effect=lambda p: captured.append(p) or "ok",
            ):
                engine.process_message(msg)

        assert captured
        assert "v1.2 shipped" in captured[-1]
        assert "@opus what shipped?" in captured[-1]
