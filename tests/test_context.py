"""Tests for memory-context injection in :func:`skchat.context.fetch_context`.

The reply path (lumina-bridge / opus-bridge / webui) assembles its prompt
from ``fetch_context``. Historically that returned only chat *history*.
These tests pin the behaviour that relevant *memory* snippets are merged in
when a memory source is available, and that the function degrades to
history-only when memory is absent.

No network: the memory source is always injected as a mock callable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def _build_history(tmp_path: Path):
    """A real ChatHistory with a small group-thread conversation."""
    from skchat.history import ChatHistory
    from skchat.models import ChatMessage

    history = ChatHistory(store=None, history_dir=tmp_path / "history")

    chef = "capauth:chef@skworld.io"
    lumina = "capauth:lumina@skworld.io"
    group = "group-mem"
    base_ts = datetime.now(timezone.utc) - timedelta(minutes=10)

    for i, (sender, content) in enumerate(
        [(chef, "did we ship the release?"), (lumina, "yes, tagged v1.2")]
    ):
        msg = ChatMessage(
            sender=sender,
            recipient=lumina if sender == chef else chef,
            content=content,
            thread_id=group,
            timestamp=base_ts + timedelta(seconds=i * 30),
        )
        msg.id = f"msg-{i}"
        history.save(msg)

    return history, chef, lumina, group


class TestMemoryContextInjection:
    def test_memory_snippets_injected_when_source_returns_hits(
        self, tmp_path: Path
    ) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        calls: list[str] = []

        def fake_memory_source(query: str, limit: int):
            calls.append(query)
            return [
                "Chef prefers concise replies",
                "release v1.2 shipped on 2026-06-09",
            ]

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
            memory_source=fake_memory_source,
        )

        # History is still present.
        assert "did we ship the release?" in ctx
        assert "tagged v1.2" in ctx

        # Memory snippets are merged in, clearly delimited.
        assert "Chef prefers concise replies" in ctx
        assert "release v1.2 shipped on 2026-06-09" in ctx
        assert "Relevant memories" in ctx

        # The memory source was actually queried.
        assert calls, "memory_source was never called"

    def test_history_only_when_memory_source_absent(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
        )

        assert "did we ship the release?" in ctx
        assert "Relevant memories" not in ctx

    def test_memory_source_failure_degrades_to_history(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        def boom(query: str, limit: int):
            raise RuntimeError("skmemory down")

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
            memory_source=boom,
        )

        assert "did we ship the release?" in ctx
        assert "Relevant memories" not in ctx

    def test_empty_memory_hits_omits_memory_block(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
            memory_source=lambda query, limit: [],
        )

        assert "did we ship the release?" in ctx
        assert "Relevant memories" not in ctx


# ---------------------------------------------------------------------------
# QA additions — pure helpers + DM lens + memory_hits/query knobs
# ---------------------------------------------------------------------------


class TestLooksLikeGroupRecipient:
    def test_bare_uuid_is_group(self) -> None:
        from skchat.context import _looks_like_group_recipient

        assert _looks_like_group_recipient("d4f3281e-fa92-474c-a8cd-f0a2a4c31c33") is True

    def test_group_scheme_is_group(self) -> None:
        from skchat.context import _looks_like_group_recipient

        assert _looks_like_group_recipient("group:anything") is True

    def test_capauth_uri_is_individual(self) -> None:
        from skchat.context import _looks_like_group_recipient

        assert _looks_like_group_recipient("capauth:lumina@skworld.io") is False

    def test_empty_is_not_group(self) -> None:
        from skchat.context import _looks_like_group_recipient

        assert _looks_like_group_recipient("") is False


class TestFormatMessage:
    def test_missing_content_returns_none(self) -> None:
        from skchat.context import _format_message

        assert _format_message({"sender": "a", "content": ""}, "self", False) is None

    def test_missing_sender_returns_none(self) -> None:
        from skchat.context import _format_message

        assert _format_message({"sender": "", "content": "hi"}, "self", False) is None

    def test_dm_line_uses_short_name(self) -> None:
        from skchat.context import _format_message

        line = _format_message(
            {"sender": "capauth:chef@skworld.io", "content": "hi",
             "recipient": "capauth:lumina@skworld.io"},
            "capauth:lumina@skworld.io",
            False,
        )
        assert line == "[chef]: hi"

    def test_group_line_gets_arrow_for_group_recipient(self) -> None:
        from skchat.context import _format_message

        line = _format_message(
            {"sender": "capauth:jarvis@skworld.io", "content": "infra green",
             "recipient": "abcdef01-2345-6789-abcd-ef0123456789"},
            "capauth:lumina@skworld.io",
            True,
        )
        assert "jarvis → group" in line


class TestFetchMemoryBlock:
    def test_empty_query_no_block(self) -> None:
        from skchat.context import _fetch_memory_block

        assert _fetch_memory_block("", lambda q, n: ["x"], 3) == ""

    def test_zero_hits_no_block(self) -> None:
        from skchat.context import _fetch_memory_block

        assert _fetch_memory_block("q", lambda q, n: ["x"], 0) == ""

    def test_source_exception_yields_empty(self) -> None:
        from skchat.context import _fetch_memory_block

        def boom(q, n):
            raise RuntimeError("down")

        assert _fetch_memory_block("q", boom, 3) == ""

    def test_renders_snippets(self) -> None:
        from skchat.context import _fetch_memory_block

        block = _fetch_memory_block("q", lambda q, n: ["alpha", "beta"], 3)
        assert "Relevant memories:" in block
        assert "- alpha" in block
        assert "- beta" in block


class TestMemoryQueryOverride:
    def test_explicit_memory_query_passed_to_source(self, tmp_path) -> None:
        """An explicit memory_query is what the source receives (not last msg)."""
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)
        seen: list[str] = []

        def source(query, limit):
            seen.append(query)
            return ["snippet"]

        fetch_context(
            self_identity=lumina, sender=chef, thread_id=group, limit=10,
            history=history, memory_source=source, memory_query="explicit query",
        )
        assert seen == ["explicit query"]

    def test_memory_hits_zero_suppresses_lookup(self, tmp_path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, group = _build_history(tmp_path)
        called = []

        ctx = fetch_context(
            self_identity=lumina, sender=chef, thread_id=group, limit=10,
            history=history, memory_source=lambda q, n: called.append(q) or ["x"],
            memory_hits=0,
        )
        assert "Relevant memories" not in ctx
        assert called == []
